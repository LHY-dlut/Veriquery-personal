"""Save responses from the real chat API for both exact-gain switch settings."""

import json
from pathlib import Path
import sys
import time

import requests


def main():
    stage, variant, port, directory, manifest = sys.argv[1:]
    root = Path(directory)
    docs = json.loads(Path(manifest).read_text(encoding="utf-8"))
    base = f"http://127.0.0.1:{port}/api/v1"
    for part, item in docs.items():
        observed = requests.get(f"{base}/documents/{item['document_id']}", timeout=20)
        observed.raise_for_status()
        document = observed.json()
        assert document["status"] == "ready" and document["chunk_count"] == item["chunk_count"], part

    with (root / f"{stage}-{variant}.jsonl").open("w", encoding="utf-8") as output:
        for line in (root / "questions.jsonl").read_text(encoding="utf-8").splitlines():
            question = json.loads(line)
            if question["stage"] != stage:
                continue
            body = {"query": question["query"], "document_ids": [docs[question["part"]]["document_id"]], "trace": True}
            start = time.monotonic()
            response = requests.post(f"{base}/chat/", json=body, timeout=120)
            response.raise_for_status()
            prediction = response.json()
            assert prediction["success"] and prediction["trace"], prediction
            row = {"question": question, "request": body, "prediction": prediction,
                   "http_seconds": round(time.monotonic() - start, 3)}
            output.write(json.dumps(row, ensure_ascii=False) + "\n")
            output.flush()
            print(stage, variant, question["id"], prediction["trace"]["answer_mode"], row["http_seconds"], flush=True)


if __name__ == "__main__":
    main()
