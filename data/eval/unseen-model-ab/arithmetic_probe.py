"""Diagnose arithmetic on source facts after all real chat requests for one arm."""

from pathlib import Path
import hashlib
import json
import sys
import time

import requests


def main():
    root = Path(sys.argv[1])
    arm, model = sys.argv[2:]
    sys.path.insert(0, str(Path.cwd()))
    from core.config import get_settings
    from core.llm_client import LLMClient

    client = LLMClient(get_settings())
    samples = [json.loads(line) for line in (root / f"{arm}.jsonl").read_text().splitlines()]
    path = root / f"arithmetic-probe-{arm}.jsonl"
    with requests.Session() as session, path.open("w", encoding="utf-8") as out:
        for row in samples:
            q = row["question"]
            if row["repeat"] != 1 or "calculation" not in q:
                continue
            text = "\n\n".join(f"[{i}] {e['part']}.pdf 原第{e['page']}页\n{e['facts']}"
                               for i, e in enumerate(q["evidence"], 1))
            prompt = client._build_prompt(q["query"], text, joint_comparison=len(q["parts"]) > 1)
            body = {"model": model, "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.1, "top_p": 0.9, "max_tokens": 256, "stream": False}
            start = time.monotonic()
            response = session.post("http://127.0.0.1:9997/v1/chat/completions", json=body, timeout=120)
            response.raise_for_status()
            result = response.json()
            assert result["model"] == model
            choice = result["choices"][0]
            raw = choice["message"].get("content") or ""
            assert isinstance(raw, str)
            record = {"id": q["id"], "arm": arm, "query": q["query"], "source_context": text,
                      "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(), "model": result["model"],
                      "raw": raw, "cleaned": client._clean_answer(raw), "usage": result.get("usage"),
                      "finish_reason": choice.get("finish_reason"), "seconds": round(time.monotonic()-start, 3)}
            out.write(json.dumps(record, ensure_ascii=False) + "\n")
            out.flush()
            print(arm, q["id"], "source arithmetic probe", flush=True)


if __name__ == "__main__":
    main()
