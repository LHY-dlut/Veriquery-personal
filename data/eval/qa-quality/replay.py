"""Replay the two frozen datasheet-rag bands through the public chat API."""
import hashlib
import json
import os
from pathlib import Path
import sys
import time

import requests

sys.path.insert(0, str(Path.cwd() / "eval"))
import run_eval as evaluation


def main():
    variant, corpus, port = sys.argv[1:]
    root = Path(os.environ["QUALITY_RUN"])
    actual = root / variant / corpus / "results"
    actual.mkdir(parents=True, exist_ok=True)
    original = Path("/root/autodl-tmp/veriquery-golden.plrkwL")
    source = json.loads((original / corpus / "results/run.json").read_text("utf8"))
    qset = original / "inputs" / (
        "exact-questions.jsonl" if corpus == "exact-advanced" else "provisional-questions.jsonl"
    )
    assert hashlib.sha256(qset.read_bytes()).hexdigest() == source["question_sha256"]
    evaluation.API = f"http://127.0.0.1:{port}"
    documents = source["documents"]
    for doc in documents.values():
        r = requests.get(f"{evaluation.API}/api/v1/documents/{doc['document_id']}", timeout=15)
        r.raise_for_status()
        observed = r.json()
        assert observed["status"] == "ready" and observed["chunk_count"] == doc["chunk_count"], doc
    (actual / "run.json").write_text(json.dumps({
        "variant": variant, "corpus": corpus, "question_sha256": source["question_sha256"],
        "documents": documents,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with (actual / "cases.jsonl").open("w", encoding="utf-8") as output:
        for question in evaluation.jsonl(qset):
            ids = [documents[p]["document_id"] for p in question["gold_parts"]]
            start = time.monotonic()
            row = evaluation.evaluate(question, ids, [], k=8, use_judge=False)
            row["elapsed_seconds"] = round(time.monotonic() - start, 3)
            output.write(json.dumps(row, ensure_ascii=False) + "\n")
            output.flush()
            prediction = row.get("prediction") or {}
            trace = prediction.get("trace") or {}
            print(variant, corpus, question["id"], trace.get("answer_mode", row["status"]),
                  prediction.get("response", "")[:130].replace("\n", " "), flush=True)


if __name__ == "__main__":
    main()
