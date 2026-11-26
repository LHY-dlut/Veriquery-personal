"""Replay the original six arithmetic questions on the old and formula-aware indexes.

Run from the application checkout that serves --port. Pass the original
unseen-model-ab/replay.py as --driver to reuse its actual HTTP replay, model
receipts and index snapshot implementation.
"""

import argparse
import importlib.util
import json
from pathlib import Path
import sys

from filelock import FileLock
import requests


ORIGINAL_QUESTIONS_SHA256 = "028b1f7d6aba60b090ade717d16986004344466fa06a8bf2bc460b7809b29ea8"
CASES = ("n02", "n03", "n04", "n06", "n07", "n10")
CORPUS = {
    "MCP4725": {"sample_sha256": "ed44bef43a0dd0d22a3267ea55f8bad1c86b21668509c9092ba4d589b100a566",
                "original_pages": [1, 15]},
    "TPS63020": {"sample_sha256": "f0ea85235544f9d6a6f06190dcd98f82fb4b59c358345ad5f38fc9d41f279780",
                 "original_pages": [6, 15]},
}
QA_FILES = ("agents/workflow_nodes.py", "api/routers/chat.py", "core/llm_client.py",
            "retrieval/hybrid_retriever.py", "retrieval/evidence_coverage.py", "core/config.py")
INGESTION_FILES = ("ingestion/mineru_client.py", "ingestion/document_processor.py",
                   "api/routers/documents.py")


def load_driver(path):
    spec = importlib.util.spec_from_file_location("unseen_model_replay_helpers", path)
    if spec is None or spec.loader is None:
        raise ValueError(f"Cannot load replay helpers from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def arithmetic_subset(helpers, source):
    raw = source.read_bytes()
    if helpers.digest(raw) != ORIGINAL_QUESTIONS_SHA256:
        raise ValueError("Original 12-question set differs from its frozen hash")
    # Preserve the exact original serialized question lines.
    selected = [(json.loads(line), line) for line in raw.splitlines(keepends=True) if line.strip()]
    selected = [(q, line) for q, line in selected if "calculation" in q]
    if tuple(q["id"] for q, _ in selected) != CASES:
        raise ValueError("Unexpected arithmetic case IDs")
    return [q for q, _ in selected], b"".join(line for _, line in selected)


def model_identity(helpers, settings):
    uid = settings.XINFERENCE_LLM_MODEL
    if uid != "qwen3.5":
        raise ValueError(f"Expected frozen 0.8B UID qwen3.5, got {uid}")
    with requests.Session() as session:
        metadata = helpers.public_model_metadata(session, settings, uid)
    size = str(metadata.get("model_size_in_billions", "")).replace("_", ".")
    if size != "0.8":
        raise ValueError(f"Unexpected registered model size: {metadata}")
    return uid


def app_files(helpers, paths):
    return {name: helpers.digest(Path(name).read_bytes()) for name in paths}


def documents_for_arm(helpers, manifest_path, settings):
    docs = helpers.read(manifest_path)
    if set(docs) != set(CORPUS):
        raise ValueError(f"Expected MCP4725 and TPS63020, found {list(docs)}")
    for part, doc in docs.items():
        uploaded = Path(settings.UPLOAD_DIR) / f"{doc['document_id']}_{doc['filename']}"
        if helpers.digest(uploaded.read_bytes()) != CORPUS[part]["sample_sha256"]:
            raise ValueError(f"{part}: persisted upload differs from the frozen 2-page PDF")
        if (doc["status"], doc["parser"], doc["parser_tier"], doc["page_count"]) != ("ready", "mineru", "advanced", 2):
            raise ValueError(f"{part}: expected two ready MinerU Advanced pages")
    return docs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--arm", required=True, choices=("before", "after"))
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--driver", required=True, type=Path)
    parser.add_argument("--documents", type=Path, help="Defaults to root/<arm>-documents.json")
    parser.add_argument("--questions", type=Path, help="Frozen original 12 questions; defaults to root/questions.jsonl")
    parser.add_argument("--inventory-only", action="store_true")
    parser.add_argument("--timeout", type=float, default=180)
    args = parser.parse_args()
    args.repeats = 3
    args.model = "qwen3.5"
    helpers = load_driver(args.driver)
    root = args.root.resolve()
    root.mkdir(exist_ok=True, parents=True)
    arm_dir = root / args.arm
    arm_dir.mkdir(exist_ok=True)
    sys.path.insert(0, str(Path.cwd()))
    from core.config import get_settings

    settings = get_settings()
    questions, selection = arithmetic_subset(helpers, args.questions or root / "questions.jsonl")
    documents = documents_for_arm(helpers, args.documents or root / f"{args.arm}-documents.json", settings)
    switches = {key: getattr(settings, key) for key in helpers.DEFAULT_SWITCHES}
    if switches != helpers.DEFAULT_SWITCHES or not settings.EVAL_TRACE_ENABLED:
        raise ValueError(f"Expected delivery switches with evaluation tracing enabled: {switches}")
    if (settings.EMBEDDING_MODEL, settings.RERANK_MODEL) != ("bge-m3", "bge-reranker-v2-m3"):
        raise ValueError("Frozen embedding or rerank model changed")
    common = {
        "original_questions_sha256": ORIGINAL_QUESTIONS_SHA256,
        "questions_sha256": helpers.digest(selection), "ids": CASES, "repeats": args.repeats,
        "corpus": CORPUS, "switches": switches, "embedding": settings.EMBEDDING_MODEL,
        "reranker": settings.RERANK_MODEL, "expected_uid": args.model,
        "source_files": app_files(helpers, QA_FILES), "driver_sha256": helpers.digest(args.driver.read_bytes()),
    }
    with FileLock(str(root / ".formula-inventory.lock"), timeout=120):
        helpers.freeze(root / "arithmetic-questions.jsonl", selection)
        helpers.freeze(root / "comparison-config.json", helpers.encoded(common))
        index = helpers.snapshot(arm_dir, documents, settings)
        manifest = {
            **common, "index": index, "ingestion_files": app_files(helpers, INGESTION_FILES),
            "documents": {part: {"id": doc["document_id"], "chunks": doc["chunk_count"]} for part, doc in documents.items()},
        }
        helpers.freeze(arm_dir / "documents.json", helpers.encoded(documents))
        helpers.freeze(arm_dir / "index-manifest.json", helpers.encoded(manifest))
    if args.inventory_only:
        print(args.arm, "verified index", index, flush=True)
        return 0
    model_identity(helpers, settings)
    with FileLock(str(root / f".{args.arm}.lock"), timeout=0):
        return helpers.run(args, settings, root, questions, documents, manifest)


if __name__ == "__main__":
    raise SystemExit(main())
