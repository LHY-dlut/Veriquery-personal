"""Upload the frozen PDFs through Veriquery and save the exact MinerU job artifacts."""

import argparse
import gzip
import hashlib
import json
from pathlib import Path
import re
import sys

import requests


def save(path, obj):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def capture(root, name, log_offset):
    with (root / "mineru.log").open("rb") as log:
        log.seek(log_offset)
        ids = dict.fromkeys(re.findall(rb"/v1/parse/jobs/(job_[a-zA-Z0-9_-]+)", log.read()))
    assert len(ids) == 1, (name, ids)
    job_id = next(iter(ids)).decode("ascii")
    base = "http://127.0.0.1:8001/v1"
    response = requests.get(f"{base}/parse/jobs/{job_id}", timeout=20)
    response.raise_for_status()
    job = response.json()
    assert job["status"] == "completed" and len(job["files"]) == 1
    file_id = job["files"][0]["output_files"]["structured_content"]["file_id"]
    response = requests.get(f"{base}/files/{file_id}/content", timeout=45)
    response.raise_for_status()
    payload = response.content
    pages = response.json()["pages"]
    assert len(pages) == 2
    with gzip.open(root / "candidate" / f"{name}-structured.json.gz", "wb") as out:
        out.write(payload)
    compact = [
        [{"index": i, "type": b.get("type"), "bbox": b.get("bbox"), "content": b.get("content")}
         for i, b in enumerate(p["blocks"])]
        for p in pages
    ]
    save(root / "candidate" / f"{name}-blocks.json", {
        "job_id": job_id, "structured_file_id": file_id, "structured_sha256": hashlib.sha256(payload).hexdigest(),
        "pages": compact,
    })


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--api", required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    sys.path.insert(0, str(Path.cwd() / "eval"))
    from run_pilot import upload

    sources = json.loads((root / "sources.json").read_text("utf-8"))
    docs = {}
    for name, item in sources.items():
        p = root / "inputs" / (name + ".pdf")
        assert hashlib.sha256(p.read_bytes()).hexdigest() == item["sample_sha256"]
        offset = (root / "mineru.log").stat().st_size
        doc = upload(args.api, p, ingest_timeout=825)
        assert (doc["status"], doc["parser"], doc["parser_tier"], doc["page_count"]) == ("ready", "mineru", "advanced", 2)
        save(root / "candidate" / (name + "-document.json"), doc)
        capture(root, name, offset)
        docs[name] = doc
        print(name, doc["document_id"], doc["chunk_count"], doc["ingestion_seconds"], flush=True)
    save(root / "candidate" / "documents.json", docs)


if __name__ == "__main__":
    main()
