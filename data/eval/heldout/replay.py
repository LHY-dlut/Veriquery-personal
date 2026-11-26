"""Replay a frozen datasheet question set, or diagnose the same LLM with gold evidence."""

import hashlib
import json
from pathlib import Path
import sys
import time

import requests


def dump(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def inventory(documents, output):
    from chromadb import PersistentClient

    store = PersistentClient(path="data/chroma").get_collection("veriquery_text")
    indexed = {}
    for part, document in documents.items():
        rows = store.get(
            where={"document_id": document["document_id"]},
            include=["documents", "metadatas"],
        )
        indexed[part] = [
            {"chunk_id": identifier, "text": text, "metadata": metadata}
            for identifier, text, metadata in zip(rows["ids"], rows["documents"], rows["metadatas"])
        ]
    dump(output, indexed)


def api_questions(questions, documents, port, output):
    base = f"http://127.0.0.1:{port}/api/v1"
    with requests.Session() as session:
        for item in documents.values():
            response = session.get(f"{base}/documents/{item['document_id']}", timeout=20)
            response.raise_for_status()
            document = response.json()
            assert document["status"] == "ready" and document["chunk_count"] == item["chunk_count"], item
        for question in questions:
            body = {
                "query": question["query"],
                "document_ids": [documents[part]["document_id"] for part in question["parts"]],
                "trace": True,
            }
            start = time.monotonic()
            response = session.post(f"{base}/chat/", json=body, timeout=130)
            response.raise_for_status()
            prediction = response.json()
            assert prediction["success"] and prediction["trace"], prediction
            row = {"question": question, "request": body, "prediction": prediction,
                   "http_seconds": round(time.monotonic() - start, 3)}
            output.write(json.dumps(row, ensure_ascii=False) + "\n")
            output.flush()
            print(question["id"], prediction["trace"]["answer_mode"], row["http_seconds"], flush=True)


def gold_context_questions(questions, settings, output):
    from core.llm_client import LLMClient

    llm = LLMClient(settings)
    for question in questions:
        if not question["evidence"]:
            continue
        context = "\n\n".join(
            f"[{number}] {item['part']}.pdf | 第{item['page']}页\n{item['facts']}"
            for number, item in enumerate(question["evidence"], 1)
        )
        answer = llm.generate_answer(
            query=question["query"], context=context, max_new_tokens=256,
            temperature=0.1, transcription=False, comparison=False,
        )
        row = {"question": question, "context": context, "model_answer": answer}
        output.write(json.dumps(row, ensure_ascii=False) + "\n")
        output.flush()
        print(question["id"], bool(answer), flush=True)


def main():
    mode, directory, port = sys.argv[1:]
    root = Path(directory)
    sys.path.insert(0, str(Path.cwd()))
    from core.config import get_settings

    settings = get_settings()
    input_file = root / "questions.jsonl"
    frozen = input_file.read_bytes()
    assert hashlib.sha256(frozen).hexdigest() == (root / "questions.sha256").read_text().strip()
    questions = [json.loads(line) for line in frozen.decode("utf-8").splitlines()]
    documents = json.loads((root / "documents.json").read_text(encoding="utf-8"))
    if mode == "inventory":
        inventory(documents, root / "indexed-chunks.json")
        return

    switches = ("RAG_FOCUS_TABLE_EVIDENCE", "RAG_BALANCED_COMPARISON", "RAG_VERIFIED_PASSAGES",
                "RAG_CONDITION_EVIDENCE", "RAG_EXACT_GAIN_EVIDENCE", "RAG_CITATION_REPAIR")
    dump(root / f"{mode}-config.json", {
        "switches": {key: getattr(settings, key) for key in switches},
        "model": settings.XINFERENCE_LLM_MODEL,
        "embedding": settings.EMBEDDING_MODEL,
        "reranker": settings.RERANK_MODEL,
        "question_sha256": hashlib.sha256(frozen).hexdigest(),
    })
    with (root / f"{mode}.jsonl").open("w", encoding="utf-8") as output:
        if mode == "gold-context":
            gold_context_questions(questions, settings, output)
        else:
            api_questions(questions, documents, port, output)


if __name__ == "__main__":
    main()
