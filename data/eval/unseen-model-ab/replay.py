"""Compare two generator UIDs through the deployed Veriquery chat endpoint."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import struct
import sys
from tempfile import NamedTemporaryFile
import time
from urllib.parse import quote
from uuid import uuid4

from filelock import FileLock
import requests


DEFAULT_SWITCHES = {
    "RAG_FOCUS_TABLE_EVIDENCE": True, "RAG_BALANCED_COMPARISON": True,
    "RAG_VERIFIED_PASSAGES": True, "RAG_CONDITION_EVIDENCE": True,
    "RAG_EXACT_GAIN_EVIDENCE": True, "RAG_CITATION_REPAIR": True,
    "RAG_EVIDENCE_COVERAGE": False, "RAG_JOINT_COMPARISON": True,
}


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")


def read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write(path, data):
    with NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
        tmp = Path(stream.name)
    tmp.replace(path)


def freeze(path, data):
    if path.exists():
        if path.read_bytes() != data:
            raise ValueError(f"Frozen data differs: {path}")
    else:
        write(path, data)


def snapshot(root, documents, settings):
    from chromadb import PersistentClient

    chroma = PersistentClient(path=str(settings.CHROMA_PERSIST_DIR))
    collection = chroma.get_collection(settings.CHROMA_COLLECTION_NAME)
    indexed = {}
    for part, item in documents.items():
        got = collection.get(where={"document_id": item["document_id"]},
                             include=["documents", "metadatas", "embeddings"])
        chunks = []
        for cid, text, meta, vector in zip(got["ids"], got["documents"], got["metadatas"], got["embeddings"], strict=True):
            if len(vector) != settings.EMBEDDING_DIMENSION:
                raise ValueError(f"Unexpected vector dimension: {cid}")
            chunks.append({"chunk_id": cid, "text": text, "metadata": meta,
                           "embedding_sha256": digest(struct.pack(f"<{len(vector)}f", *vector))})
        if len(chunks) != item["chunk_count"] or not chunks:
            raise ValueError(f"Document {part} expected {item['chunk_count']} chunks, observed {len(chunks)}")
        indexed[part] = sorted(chunks, key=lambda row: row["chunk_id"])
    document_ids = [doc["document_id"] for doc in documents.values()]
    db = Path(getattr(settings, "table_db_path", None) or "./data/tables.db").resolve()
    with sqlite3.connect(db.as_uri() + "?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        placeholders = ",".join("?" for _ in document_ids)
        tables = [dict(row) for row in connection.execute(
            f"SELECT * FROM tables WHERE document_id IN ({placeholders}) ORDER BY table_id", document_ids,
        )]
    block_bytes, table_bytes = encoded(indexed), encoded(tables)
    freeze(root / "indexed.json", block_bytes)
    freeze(root / "indexed-tables.json", table_bytes)
    return {"chroma_chunks_sha256": digest(block_bytes), "table_rows_sha256": digest(table_bytes),
            "bm25_sha256": digest((settings.DATA_DIR / "bm25_index.pkl").read_bytes())}


def public_model_metadata(session, settings, uid):
    url = str(settings.XINFERENCE_API_BASE).rstrip("/") + "/models/" + quote(uid, safe="")
    key = settings.XINFERENCE_API_KEY
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    response = session.get(url, headers=headers, timeout=30)
    response.raise_for_status()
    info = response.json()
    fields = ("id", "model_uid", "model_name", "model_family", "model_size_in_billions",
              "model_engine", "model_format", "quantization", "context_length", "accelerators")
    return {field: info[field] for field in fields if field in info}


def tap_receipts(path, sample_id, attempt, uid, trace):
    entries = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            if record.get("sample_id") == sample_id and record.get("attempt") == attempt:
                entries.append(record)
    generated = trace.get("model_answer") is not None or any(
        call.get("draft") is not None for call in trace.get("comparison_calls") or []
    )
    successful = []
    for row in entries:
        if row["requested_model"] != uid:
            raise ValueError(f"{sample_id}: downstream request used {row['requested_model']} instead of {uid}")
        if row["success"]:
            if row.get("returned_model") != uid or not re.fullmatch(r"[a-f0-9]{64}", row.get("prompt_sha256") or ""):
                raise ValueError(f"{sample_id}: inconsistent downstream response UID or missing prompt digest")
            successful.append(row)
    if generated and not successful:
        raise ValueError(f"{sample_id}: trace shows generation but no successful downstream receipt")
    return [{"requested_model": r["requested_model"], "returned_model": r["returned_model"],
             "prompt_sha256": r["prompt_sha256"]} for r in successful]


def publish(root, arm, samples):
    rows = []
    for sample_id, _, _ in samples:
        path = root / arm / (sample_id + ".json")
        if path.exists():
            rows.append(read(path))
    write(root / f"{arm}.jsonl", b"".join(encoded(row) for row in rows))
    return rows


def run(args, settings, root, questions, documents, common):
    uid = settings.XINFERENCE_LLM_MODEL
    if args.model and args.model != uid:
        raise ValueError(f"Expected {args.model} in driver environment, got {uid}")
    if not settings.EVAL_TRACE_ENABLED:
        raise ValueError("EVAL_TRACE_ENABLED must be true for the API and driver")
    arm = args.arm
    samples = [(f"{q['id']}-r{rep}", rep, q) for rep in range(1, args.repeats + 1) for q in questions]
    base = f"http://127.0.0.1:{args.port}/api/v1"
    dest = root / arm
    dest.mkdir(exist_ok=True)
    with requests.Session() as session:
        metadata = public_model_metadata(session, settings, uid)
        manifest = {**common, "model": uid, "backend_model": metadata, "arm": arm, "port": args.port}
        freeze(root / f"{arm}-config.json", encoded(manifest))
        for part, doc in documents.items():
            response = session.get(f"{base}/documents/{doc['document_id']}", timeout=20)
            response.raise_for_status()
            actual = response.json()
            for key in ("document_id", "filename", "chunk_count", "page_count", "parser", "parser_tier"):
                if doc.get(key) != actual.get(key):
                    raise ValueError(f"{part}: frozen/served {key} differs")
            if actual["status"] != "ready" or actual["parser_tier"] != "advanced":
                raise ValueError(f"{part}: document is not ready at Advanced tier")
        for sample_id, repeat, q in samples:
            case_id = q["id"]
            path = dest / f"{sample_id}.json"
            if path.is_file() and read(path)["success"]:
                continue
            attempt = 1
            while (dest / f"{sample_id}.attempt-{attempt}.json").exists():
                attempt += 1
            session_id = str(uuid4())
            body = {"query": q["query"], "document_ids": [documents[p]["document_id"] for p in q["parts"]],
                    "session_id": session_id, "trace": True}
            marker = root / f"active-{arm}.json"
            label = {"arm": arm, "id": case_id, "case_id": case_id, "sample_id": sample_id, "repeat": repeat,
                     "attempt": attempt, "session_id": session_id}
            write(marker, encoded(label))
            ledger = dest / f"{sample_id}.attempt-{attempt}.json"
            start = time.monotonic()
            row = {"question": q, "request": body, **label,
                   "started_utc": now(), "success": False, "model": uid}
            write(ledger, encoded(row))
            try:
                response = session.post(f"{base}/chat/", json=body, timeout=args.timeout)
                row["http_status"] = response.status_code
                row["prediction"] = response.json()
                response.raise_for_status()
                prediction = row["prediction"]
                if not prediction.get("success") or not isinstance(prediction.get("trace"), dict):
                    raise ValueError("Chat failed or did not return a trace")
                if prediction.get("session_id") != session_id:
                    raise ValueError("Session ID mismatch")
                row["model_calls"] = tap_receipts(root / f"model-calls-{arm}.jsonl", sample_id, attempt, uid,
                                                 prediction["trace"])
                row["success"] = True
            except (ValueError, KeyError, requests.RequestException) as exc:
                row["error"] = f"{type(exc).__name__}: {exc}"
            finally:
                row["http_seconds"] = round(time.monotonic() - start, 3)
                write(ledger, encoded(row))
                write(path, encoded(row))
                marker.unlink(missing_ok=True)
            print(arm, sample_id, "ok" if row["success"] else row["error"], row["http_seconds"], flush=True)
            publish(root, arm, samples)
    results = publish(root, arm, samples)
    return 0 if len(results) == len(samples) and all(x["success"] for x in results) else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arm", choices=("inventory", "current", "stronger"), required=True)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--model", help="Expected XINFERENCE_LLM_MODEL UID")
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    root = args.root.resolve()
    sys.path.insert(0, str(Path.cwd()))
    from core.config import get_settings

    settings = get_settings()
    raw = (root / "questions.jsonl").read_bytes()
    expected = (root / "questions.sha256").read_text(encoding="utf-8-sig").split()[0].lower()
    if digest(raw) != expected:
        raise ValueError("Question set differs from frozen SHA-256")
    questions = [json.loads(line) for line in raw.decode("utf-8-sig").splitlines() if line.strip()]
    documents = read(root / "documents.json")
    ids = [q["id"] for q in questions]
    if not ids or len(ids) != len(set(ids)) or not all(re.fullmatch(r"[\w-]+", i) for i in ids):
        raise ValueError("Invalid question IDs")
    if any(not q["parts"] or any(key not in documents for key in q["parts"]) for q in questions):
        raise ValueError("Question points outside the frozen document set")
    switches = {key: getattr(settings, key) for key in DEFAULT_SWITCHES}
    if switches != DEFAULT_SWITCHES:
        raise ValueError(f"Experiment requires the delivery switches: {switches}")
    with FileLock(str(root / ".inventory.lock"), timeout=120):
        ids_and_counts = {k: {"id": doc["document_id"], "chunks": doc["chunk_count"]}
                          for k, doc in documents.items()}
        source = {p: digest(Path(p).read_bytes()) for p in ("agents/workflow_nodes.py", "api/routers/chat.py",
                  "retrieval/hybrid_retriever.py", "retrieval/evidence_coverage.py", "core/llm_client.py", "core/config.py")}
        common = {"switches": switches, "questions_sha256": expected, "documents": ids_and_counts,
                  "repeats": args.repeats, "source_files": source,
                  "embedding": settings.EMBEDDING_MODEL, "reranker": settings.RERANK_MODEL,
                  "index": snapshot(root, documents, settings)}
        freeze(root / "comparison-config.json", encoded(common))
    if args.arm == "inventory":
        print("Verified frozen index", common["index"], flush=True)
        return 0
    with FileLock(str(root / f".{args.arm}.lock"), timeout=0):
        return run(args, settings, root, questions, documents, common)


if __name__ == "__main__":
    raise SystemExit(main())
