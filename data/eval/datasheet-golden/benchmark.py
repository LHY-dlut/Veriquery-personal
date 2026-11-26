"""Replay unchanged datasheet-rag questions against actual HTTP endpoints."""
import hashlib
import json
import os
from pathlib import Path
import sys
import time

import requests

sys.path.insert(0, str(Path.cwd() / "eval"))
import run_eval as evaluation
from run_pilot import upload


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    phase, port = sys.argv[1:]
    root = Path(os.environ["GOLDEN_RUN"])
    inputs = root / "inputs"
    plan = json.loads((inputs / "benchmark-plan.json").read_text("utf-8"))
    exact = phase == "exact-advanced"
    band = "exact" if exact else "provisional"
    question_file = inputs / f"{band}-questions.jsonl"
    assert digest(question_file) == plan["selection"][band]["question_sha256"]
    questions = evaluation.jsonl(question_file)
    assert [q["id"] for q in questions] == plan["selection"][band]["ids"]
    evaluation.API = f"http://127.0.0.1:{port}"
    if exact:
        docs = {}
        for label, item in plan["exact"].items():
            path = inputs / "inputs" / item["sample"]
            assert digest(path) == item["sample_sha256"]
            entry = upload(evaluation.API, path, ingest_timeout=780)
            assert entry["parser"] == "mineru" and entry["parser_tier"] == "advanced", entry
            docs[label] = entry
            print(phase, "ready", label, entry["document_id"], entry["ingestion_seconds"], flush=True)
        provenance = plan["exact"]
    elif phase == "provisional-before":
        source = Path("/root/autodl-tmp/veriquery-delivery.bLSCZA/reviewed/candidate/results/run.json")
        prior = json.loads(source.read_text("utf-8"))
        docs = prior["documents"]
        provenance = prior["corpus"]
        for part, item in provenance.items():
            assert digest(Path(item["path"])) == item["sha256"], part
        path = Path("/root/autodl-tmp/veriquery-corpus/opa211-pages-1-9.pdf")
        entry = upload(evaluation.API, path, ingest_timeout=360)
        assert entry["parser_tier"] == "standard", entry
        docs["OPA211"] = entry
        provenance["OPA211"] = {"path": str(path), "sha256": digest(path), "original_pages": [1, 9]}
        print(phase, "ready", "OPA211", entry["document_id"], flush=True)
    else:
        earlier = json.loads((root / "provisional-before/results/run.json").read_text("utf-8"))
        docs = earlier["documents"]
        provenance = earlier["provenance"]
    for part, doc in docs.items():
        item = requests.get(f"{evaluation.API}/api/v1/documents/{doc['document_id']}", timeout=20)
        item.raise_for_status()
        checked = item.json()
        assert checked["status"] == "ready" and checked["chunk_count"] == doc["chunk_count"], (part, checked)
    results = root / phase / "results"
    results.mkdir(parents=True, exist_ok=True)
    details = {"phase": phase, "question_sha256": digest(question_file), "documents": docs,
               "provenance": provenance, "exact_pdf_source": exact, "scoped_pages": True}
    (results / "run.json").write_text(json.dumps(details, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    rows = []
    with (results / "cases.jsonl").open("w", encoding="utf-8") as trace:
        for case in questions:
            ids = list(dict.fromkeys(docs[part]["document_id"] for part in case["gold_parts"]))
            start = time.monotonic()
            result = evaluation.evaluate(case, ids, [], k=8, use_judge=False)
            result["http_seconds"] = round(time.monotonic() - start, 3)
            rows.append(result)
            trace.write(json.dumps(result, ensure_ascii=False) + "\n")
            trace.flush()
            print(phase, case["id"], result["status"],
                  result.get("prediction", {}).get("response", "")[:110].replace("\n", " "), flush=True)
    evaluation.report(results / "metrics.md", rows, k=8, judge_enabled=False)


if __name__ == "__main__":
    main()
