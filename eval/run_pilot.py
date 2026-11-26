#!/usr/bin/env python3
"""Upload a frozen corpus into an isolated test instance and call real chat."""
import argparse
import hashlib
import json
from pathlib import Path
import time

import requests

import run_eval as evaluation


def upload(api, path, ingest_timeout):
    started = time.monotonic()
    with path.open("rb") as stream:
        response = requests.post(
            f"{api}/api/v1/documents/upload",
            files={"file": (path.name, stream, "application/pdf")},
            timeout=(15, 240),
        )
    response.raise_for_status()
    doc_id = response.json()["document_id"]
    deadline = started + ingest_timeout
    poll_seconds = []
    poll_timeouts = 0
    while time.monotonic() < deadline:
        sampled_at = time.monotonic()
        try:
            response = requests.get(f"{api}/api/v1/documents/{doc_id}", timeout=20)
        except requests.Timeout:
            poll_timeouts += 1
            continue
        poll_seconds.append(time.monotonic() - sampled_at)
        response.raise_for_status()
        doc = response.json()
        if doc["status"] == "ready":
            doc["ingestion_seconds"] = round(time.monotonic() - started, 3)
            doc["longest_status_request_seconds"] = round(max(poll_seconds), 3)
            doc["status_poll_timeouts"] = poll_timeouts
            return doc
        if doc["status"] in {"error", "failed"}:
            raise RuntimeError(f"ingestion failed: {doc}")
        time.sleep(1)
    raise TimeoutError(f"Timed out waiting for {doc_id}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", required=True)
    parser.add_argument("--corpus", required=True, type=Path, help="part → {path, sha256, original_pages}")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--label", required=True)
    parser.add_argument("--tier", default="advanced")
    parser.add_argument("--ingest-timeout", default=1900, type=int)
    parser.add_argument("--golden", default=Path(__file__).with_name("pilot_cases.jsonl"), type=Path)
    args = parser.parse_args()
    evaluation.API = args.api.rstrip("/")
    corpus = json.loads(args.corpus.read_text("utf-8"))
    questions = evaluation.jsonl(args.golden)
    parts = sorted({p for item in questions for p in item["gold_parts"]})
    for part in parts:
        item = corpus[part]
        if hashlib.sha256(Path(item["path"]).read_bytes()).hexdigest() != item["sha256"]:
            raise ValueError(f"PDF hash does not match the frozen manifest: {part}")

    args.out.mkdir(parents=True, exist_ok=True)
    docs = {}
    for part in parts:
        doc = upload(evaluation.API, Path(corpus[part]["path"]), args.ingest_timeout)
        if doc.get("parser") != "mineru" or doc.get("parser_tier") != args.tier:
            raise RuntimeError(f"Unexpected parser fallback: {part} {doc}")
        docs[part] = doc
        print(f"{args.label} ready {part}: {doc['document_id']} "
              f"{doc['chunk_count']} chunks, {doc['verified_table_rows']} verified rows", flush=True)
    (args.out / "run.json").write_text(json.dumps({
        "label": args.label,
        "corpus": corpus,
        "questions_sha256": hashlib.sha256(args.golden.read_bytes()).hexdigest(),
        "documents": docs,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    rows = []
    with (args.out / "cases.jsonl").open("w", encoding="utf-8") as trace:
        for case in questions:
            scope = [docs[part]["document_id"] for part in case["gold_parts"]]
            gold = [{**item, "document_id": docs[item["part"]]["document_id"]}
                    for item in case.get("evidence", [])]
            start = time.monotonic()
            row = evaluation.evaluate(case, scope, gold, k=8, use_judge=False)
            row["http_seconds"] = round(time.monotonic() - start, 3)
            rows.append(row)
            trace.write(json.dumps(row, ensure_ascii=False) + "\n")
            trace.flush()
            print(f"{args.label} {case['id']} {row['status']} {row['http_seconds']}s", flush=True)
    evaluation.report(args.out / "metrics.md", rows, k=8, judge_enabled=False)
    return int(any(row["status"] == "service_error" for row in rows))


if __name__ == "__main__":
    raise SystemExit(main())
