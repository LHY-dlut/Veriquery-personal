"""Preview or check the Xinference endpoints configured for Veriquery."""

import argparse
import base64
import io
import math
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

from dotenv import dotenv_values
import requests


ROOT = Path(__file__).resolve().parents[1]
MODEL_APIS = {
    "embedding": ("EMBEDDING_MODEL", "embeddings"),
    "rerank": ("RERANK_MODEL", "rerank"),
    "llm": ("XINFERENCE_LLM_MODEL", "chat/completions"),
    "caption": ("XINFERENCE_VLM_MODEL", "chat/completions"),
    "visual-embedding": ("VL_EMBEDDING_MODEL", "embeddings"),
}
TEXT_STAGES = ("embedding", "rerank", "llm")
VOLTAGE_EVIDENCE = "An electronic component's operating voltage is 3.3 V."
UNRELATED_EVIDENCE = "Penguins live in colonies."


def unit_vector(values, dimension):
    if not isinstance(values, list) or len(values) != dimension:
        raise ValueError(f"Expected an embedding of dimension {dimension}")
    if any(type(value) not in (int, float) or not math.isfinite(value) for value in values):
        raise ValueError("Embedding contains invalid numbers")
    norm = math.hypot(*values)
    if not math.isfinite(norm) or norm <= 1e-8:
        raise ValueError("Embedding has an invalid norm")
    return [value / norm for value in values]


def check_vectors(vectors, dimension):
    first, second = [unit_vector(vector, dimension) for vector in vectors]
    if math.fsum(x * y for x, y in zip(first, second)) >= 0.99:
        raise ValueError("Different inputs produced nearly identical embeddings")


def image_content(path):
    from PIL import Image

    buffer = io.BytesIO()
    with Image.open(path) as image:
        image.convert("RGB").save(buffer, format="PNG")
    data = base64.b64encode(buffer.getvalue()).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{data}"}}


def chat_content(call, uid, content):
    body = call("chat/completions", {
        "model": uid, "stream": False, "max_tokens": 256,
        "messages": [{"role": "user", "content": content}],
    })
    reply = body["choices"][0]["message"].get("content")
    if isinstance(reply, list):
        reply = "".join(part.get("text", "") for part in reply if isinstance(part, dict))
    if not isinstance(reply, str) or not reply.strip():
        raise ValueError("Chat response has no final content")
    return reply


def check_embedding(call, uid, dimension):
    result = call("embeddings", {"model": uid, "input": [VOLTAGE_EVIDENCE, UNRELATED_EVIDENCE]})["data"]
    if len(result) != 2 or {item["index"] for item in result} != {0, 1}:
        raise ValueError("Embedding result must return both input indices")
    check_vectors([item["embedding"] for item in result], dimension)
    print(f"PASS {uid} /embeddings: two distinct {dimension}-dimensional vectors")


def check_rerank(call, uid):
    result = call("rerank", {
        "model": uid, "query": "What is the component operating voltage?",
        "documents": [UNRELATED_EVIDENCE, VOLTAGE_EVIDENCE],
    })["results"]
    scores = {row["index"]: float(row["relevance_score"]) for row in result}
    if len(result) != 2 or set(scores) != {0, 1} or not all(map(math.isfinite, scores.values())):
        raise ValueError("Rerank result must contain a finite score for each input")
    if scores[1] <= scores[0]:
        raise ValueError("Reranker did not rank the voltage evidence first")
    print(f"PASS {uid} /rerank: relevant document ranked first")


def check_llm(call, uid):
    answer = chat_content(call, uid, f"{VOLTAGE_EVIDENCE} What is the voltage? Reply with the value.")
    if not re.search(r"(?<![\d.])3\.3(?![\d.])", answer):
        raise ValueError("Chat result did not return the expected voltage")
    print(f"PASS {uid} /chat/completions: returned the expected voltage in final content")


def check_visual_embedding(call, uid, images):
    def embed(item):
        return call("embeddings", {"model": uid, "input": item})["data"][0]["embedding"]

    variants = ["An amplifier output characteristics curve.", "A package outline mechanical drawing."]
    check_vectors([embed(value) for value in variants], 2048)
    print(f"PASS {uid} /embeddings: distinct text vectors")
    if images:
        payloads = [{"role": "user", "content": [image_content(path)]} for path in images]
        check_vectors([embed(value) for value in payloads], 2048)
        print(f"PASS {uid} /embeddings: distinct image vectors for the Veriquery payload")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=("text", *MODEL_APIS), default="text")
    parser.add_argument("--check", action="store_true", help="Send requests to a running service")
    parser.add_argument("--image", type=Path, action="append", default=[],
                        help="One PNG/JPEG for caption or two different images for visual embedding")
    args = parser.parse_args()
    if args.image and (not args.check or args.stage not in ("caption", "visual-embedding")):
        parser.error("--image requires --check with a visual stage")
    if args.check and args.stage == "caption" and len(args.image) != 1:
        parser.error("caption check needs exactly one --image")
    if args.stage == "visual-embedding" and len(args.image) not in (0, 2):
        parser.error("visual embedding image check needs two different --image inputs")

    settings = {**dotenv_values(ROOT / ".env.example"), **dotenv_values(ROOT / ".env"), **os.environ}
    stages = TEXT_STAGES if args.stage == "text" else (args.stage,)
    expected = [MODEL_APIS[stage] for stage in stages]
    try:
        base = settings["XINFERENCE_API_BASE"].rstrip("/")
        url = urlsplit(base)
        if (url.scheme not in ("http", "https") or not url.hostname or url.username is not None
                or url.query or url.fragment or not url.path.rstrip("/").endswith("/v1")):
            raise ValueError("XINFERENCE_API_BASE must be an HTTP(S) base ending in /v1")
        timeout = int(settings["XINFERENCE_TIMEOUT"])
        if timeout <= 0:
            raise ValueError("XINFERENCE_TIMEOUT must be positive")
        for key, path in expected:
            if not settings.get(key):
                raise ValueError(f"Missing model UID in {key}")
            print(f"{key}={settings[key]}  POST /v1/{path}")
        if not args.check:
            print("Offline configuration preview. No requests sent.")
            return 0

        with requests.Session() as session:
            if url.hostname in ("localhost", "127.0.0.1", "::1"):
                session.trust_env = False
            key = settings.get("XINFERENCE_API_KEY")
            if key:
                session.headers["Authorization"] = f"Bearer {key}"

            def call(path, payload=None):
                method = "POST" if payload is not None else "GET"
                try:
                    result = session.request(method, f"{base}/{path}", json=payload,
                                             timeout=15 if path == "rerank" else timeout,
                                             allow_redirects=False)
                except requests.RequestException as exc:
                    raise ValueError(f"{method} {path}: {type(exc).__name__}") from exc
                if not 200 <= result.status_code < 300:
                    raise ValueError(f"{method} {path}: HTTP {result.status_code}")
                return result.json()

            registered = {model["id"] for model in call("models")["data"]}
            missing = [settings[key] for key, _ in expected if settings[key] not in registered]
            if missing:
                raise ValueError(f"Missing model UIDs in /v1/models: {', '.join(missing)}")
            print("PASS /models: expected model UIDs are visible")
            for stage in stages:
                key, _ = MODEL_APIS[stage]
                uid = settings[key]
                if stage == "embedding":
                    check_embedding(call, uid, int(settings["EMBEDDING_DIMENSION"]))
                elif stage == "rerank":
                    check_rerank(call, uid)
                elif stage == "llm":
                    check_llm(call, uid)
                elif stage == "caption":
                    content = [image_content(args.image[0]), {"type": "text", "text": "Describe this image briefly."}]
                    answer = chat_content(call, uid, content)
                    print(f"PASS {uid} /chat/completions: image request returned final text ({len(answer)} characters)")
                else:
                    check_visual_embedding(call, uid, args.image)
    except (AttributeError, ImportError, IndexError, KeyError, OSError, OverflowError, TypeError, ValueError) as exc:
        print(f"FAIL {args.stage}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
