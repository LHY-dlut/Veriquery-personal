"""Diagnose generation length separately on unchanged post-fix model inputs."""

import hashlib
import json
from pathlib import Path
import sys

import requests


def main():
    root = Path(sys.argv[1])
    sys.path.insert(0, str(Path.cwd()))
    from core.config import get_settings
    from core.llm_client import LLMClient

    client = LLMClient(get_settings())
    rows = list(map(json.loads, (root / "after.jsonl").read_text("utf-8").splitlines()))
    calls = list(map(json.loads, (root / "model-calls-after.jsonl").read_text("utf-8").splitlines()))
    with requests.Session() as session, (root/"budget-check.jsonl").open("w", encoding="utf-8") as f:
        for row in rows:
            if row["repeat"] != 1:
                continue
            t = row["prediction"]["trace"]
            q = row["question"]
            prompt = client._build_prompt(q["query"], t["context"], joint_comparison=t["answer_mode"]=="joint_comparison")
            sig = hashlib.sha256(prompt.encode()).hexdigest()
            first = next(c for c in calls if c["sample_id"] == row["sample_id"] and c["success"])
            assert sig == first["prompt_sha256"]
            for limit in (256, 768):
                req = {"model": "qwen3.5", "messages": [{"role": "user", "content": prompt}],
                       "max_tokens": limit, "temperature": 0, "top_p": 1, "stream": False}
                resp = session.post("http://127.0.0.1:9997/v1/chat/completions", json=req, timeout=150)
                resp.raise_for_status()
                body = resp.json()
                assert body["model"]=="qwen3.5"
                choice = body["choices"][0]
                raw = choice["message"]["content"]
                f.write(json.dumps({"id":q["id"], "model":body["model"], "limit":limit, "prompt_sha256":sig,
                                    "temperature":0,"top_p":1,"raw":raw,"cleaned":client._clean_answer(raw),
                                    "usage":body["usage"],"finish":choice["finish_reason"]}, ensure_ascii=False)+"\n")
                f.flush()
                print(q["id"], limit, body["usage"]["completion_tokens"], flush=True)


if __name__ == "__main__":
    main()
