"""Replay four frozen prompt pairs, one model arm per invocation."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
from tempfile import NamedTemporaryFile
import time
from types import SimpleNamespace

import requests


CONTEXTS = {"original": "original_model_context", "short": "short_reviewed_context"}
GENERATION = {"max_tokens": 256, "temperature": 0.1, "top_p": 0.9, "stream": False}
CASE_IDS = ["h03", "h04", "h05", "h07"]


def digest(data):
    return hashlib.sha256(data).hexdigest()


def encode(data):
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


def write_atomic(path, data):
    with NamedTemporaryFile("w", encoding="utf-8", newline="\n", dir=path.parent, delete=False) as file:
        file.write(data)
        file.flush()
        os.fsync(file.fileno())
        tmp = Path(file.name)
    tmp.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--arm", required=True, choices=("current", "stronger"))
    parser.add_argument("--base-url", required=True, help="OpenAI-compatible API base including /v1")
    parser.add_argument("--model", required=True)
    parser.add_argument("--model-config", type=Path, help="Public deployment/weights metadata in JSON format")
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--api-key-env", default="XINFERENCE_API_KEY")
    args = parser.parse_args()

    sys.path.insert(0, str(args.repo.resolve()))
    from core.llm_client import LLMClient

    api = args.base_url.rstrip("/")
    client = LLMClient(SimpleNamespace(
        XINFERENCE_API_BASE=api, XINFERENCE_TIMEOUT=args.timeout, XINFERENCE_LLM_MODEL=args.model,
        XINFERENCE_API_KEY=os.environ.get(args.api_key_env, ""),
    ))
    source = args.input.read_bytes()
    controls = [json.loads(line) for line in source.decode("utf-8-sig").splitlines() if line.strip()]
    if [row["id"] for row in controls] != CASE_IDS:
        parser.error("Expected frozen cases h03, h04, h05, h07 in order")
    deployment = json.loads(args.model_config.read_text("utf-8-sig")) if args.model_config else None
    manifest = {
        "arm": args.arm, "api_base": api, "requested_model": args.model, "generation": GENERATION,
        "deployment": deployment, "controls_sha256": digest(source),
        "llm_client_sha256": digest((args.repo / "core/llm_client.py").read_bytes()),
        "repetitions": 3, "context_fields": CONTEXTS,
    }
    dest = args.out / args.arm
    dest.mkdir(parents=True, exist_ok=True)
    manifest_file = dest / "manifest.json"
    if manifest_file.exists() and json.loads(manifest_file.read_text("utf-8")) != manifest:
        parser.error("Existing arm manifest differs; use a separate output directory")
    write_atomic(manifest_file, encode(manifest) + "\n")

    with requests.Session() as session:
        session.headers.update(client._headers)
        for rep in range(1, 4):
            for case in controls:
                for context, field in CONTEXTS.items():
                    sample_id = f"{case['id']}-{context}-r{rep}"
                    path = dest / f"{sample_id}.json"
                    if path.exists() and json.loads(path.read_text("utf-8")).get("success"):
                        continue
                    prompt = client._build_prompt(
                        case["query"], case[field], joint_comparison=case["joint_comparison"],
                    )
                    payload = {"model": args.model, "messages": [{"role": "user", "content": prompt}], **GENERATION}
                    row = {
                        "sample_id": sample_id, "arm": args.arm, "id": case["id"], "context": context,
                        "repeat": rep, "query": case["query"], "rubric": case["rubric"],
                        "joint_comparison": case["joint_comparison"], "request": payload,
                        "context_sha256": digest(case[field].encode()), "prompt_sha256": digest(prompt.encode()),
                        "started_utc": datetime.now(timezone.utc).isoformat(), "success": False,
                    }
                    start = time.monotonic()
                    try:
                        response = session.post(f"{api}/chat/completions", json=payload, timeout=args.timeout)
                        row["http_status"] = response.status_code
                        response.raise_for_status()
                        result = response.json()
                        choice = result["choices"][0]
                        message = choice["message"]
                        raw = message.get("content")
                        if isinstance(raw, list):
                            raw = "".join(part.get("text", "") for part in raw if isinstance(part, dict))
                        if raw is None and (message.get("reasoning_content") or choice.get("finish_reason") == "length"):
                            raw = ""
                        if not isinstance(raw, str):
                            raise ValueError("Completion has no textual assistant content")
                        row.update(
                            success=True, response_id=result.get("id"), returned_model=result.get("model"),
                            system_fingerprint=result.get("system_fingerprint"), raw_message=message, raw_answer=raw,
                            cleaned_answer=client._clean_answer(raw.strip()), finish_reason=choice.get("finish_reason"),
                            usage=result.get("usage"), answered=bool(raw.strip()),
                        )
                    except (requests.RequestException, ValueError, KeyError, IndexError, TypeError) as exc:
                        row.update(error_type=type(exc).__name__, error=str(exc))
                    row["elapsed_seconds"] = round(time.monotonic() - start, 3)
                    write_atomic(path, encode(row) + "\n")
                    print(args.arm, sample_id, row["success"], row.get("finish_reason"), row["elapsed_seconds"], flush=True)
                    publish(dest, args.out / f"{args.arm}.jsonl")
    rows = publish(dest, args.out / f"{args.arm}.jsonl")
    return 0 if len(rows) == 24 and all(row["success"] for row in rows) else 1


def publish(directory, output):
    rows = [json.loads(path.read_text("utf-8")) for path in sorted(directory.glob("h*-r*.json"))]
    write_atomic(output, "".join(encode(row) + "\n" for row in rows))
    return rows


if __name__ == "__main__":
    raise SystemExit(main())
