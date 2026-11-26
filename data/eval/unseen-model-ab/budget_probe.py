"""Post-hoc budget sensitivity for gold-fact prompts that hit 256 tokens on both arms."""

from pathlib import Path
import hashlib
import json
import sys

import requests


def main():
    root = Path(sys.argv[1])
    arm, uid = sys.argv[2:]
    sys.path.insert(0, str(Path.cwd()))
    from core.config import get_settings
    from core.llm_client import LLMClient

    client = LLMClient(get_settings())
    questions = {r["id"]: r for r in map(json.loads, (root / "questions.jsonl").read_text().splitlines())}
    prior = {name: {r["id"]: r for r in map(json.loads, (root/f"arithmetic-probe-{name}.jsonl").read_text().splitlines())}
             for name in ("current", "stronger")}
    cases = [i for i in questions if all(i in by_id and by_id[i]["usage"]["completion_tokens"] == 256
                                         for by_id in prior.values())]
    assert cases == ["n02", "n04", "n06"]
    with requests.Session() as session, (root / f"budget-{arm}.jsonl").open("w", encoding="utf-8") as out:
        for i in cases:
            p = prior[arm][i]
            prompt = client._build_prompt(p["query"], p["source_context"])
            sig = hashlib.sha256(prompt.encode()).hexdigest()
            assert sig == p["prompt_sha256"]
            for limit in (256, 768):
                body = {"model": uid, "messages": [{"role": "user", "content": prompt}], "temperature": 0.0,
                        "top_p": 1.0, "stream": False, "max_tokens": limit}
                response = session.post("http://127.0.0.1:9997/v1/chat/completions", json=body, timeout=150)
                response.raise_for_status()
                obj = response.json()
                assert obj["model"] == uid
                choice = obj["choices"][0]
                raw = choice["message"]["content"]
                out.write(json.dumps({"id": i, "uid": uid, "limit": limit, "temperature": 0, "top_p": 1,
                                      "prompt_sha256": sig, "usage": obj["usage"], "finish_reason": choice["finish_reason"],
                                      "raw": raw, "cleaned": client._clean_answer(raw)}, ensure_ascii=False) + "\n")
                out.flush()
                print(arm, i, limit, obj["usage"]["completion_tokens"], flush=True)


if __name__ == "__main__":
    main()
