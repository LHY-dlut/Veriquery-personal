"""Prepare masked trace packets and join per-sample judgements for the six arithmetic cases."""

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
from random import Random
from statistics import median


ARMS = ("before", "after")
CASES = ("n02", "n03", "n04", "n06", "n07", "n10")


def read_lines(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]


def dump(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def fingerprint(data):
    raw = json.dumps(data, sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha256(raw).hexdigest()[:20]


def read_arm(root, arm):
    rows = read_lines(root / f"{arm}.jsonl")
    expected = {(qid, rep) for qid in CASES for rep in (1, 2, 3)}
    if len(rows) != 18 or {(r["id"], r["repeat"]) for r in rows} != expected:
        raise ValueError(f"Expected 18 distinct case/repeat tuples in {arm}")
    if any(not r["success"] or not r["prediction"]["success"] for r in rows):
        raise ValueError(f"Unsuccessful API response in {arm}; complete the affected attempts before scoring")
    return rows


def packets(root):
    dest = root / "blind"
    dest.mkdir(exist_ok=True)
    questions, mapping = {}, {}
    by_question = {qid: [] for qid in CASES}
    for arm in ARMS:
        contents = json.loads((root / arm / "indexed.json").read_text(encoding="utf-8"))
        inventory = {
            part: [{"id": row["chunk_id"], "page": row["metadata"]["page"],
                    "source": row["metadata"].get("source"), "text": row["text"]} for row in rows]
            for part, rows in contents.items()
        }
        inventory_name = f"index-{fingerprint(inventory)}.json"
        dump(dest / inventory_name, inventory)
        receipt_rows = read_lines(root / f"model-calls-{arm}.jsonl")
        for row in read_arm(root, arm):
            q = row["question"]
            if questions.setdefault(q["id"], q) != q:
                raise ValueError(f"Question changed: {q['id']}")
            prediction = row["prediction"]
            t = prediction["trace"]
            material = {
                "inventory": inventory_name,
                "input": t.get("context"),
                "context_evidence": t.get("context_evidence"),
                "retrieved": [{"id": v["chunk_id"], "document_id": v["document_id"],
                               "page": v["page"], "text": v["text"]} for v in t.get("retrieved") or []],
                "condition_candidates": t.get("condition_candidates"),
                "comparison_calls": t.get("comparison_calls"),
            }
            calls = [r for r in receipt_rows if r.get("success") and r["sample_id"] == row["sample_id"]
                     and r["attempt"] == row["attempt"]]
            if any(c["requested_model"] != "qwen3.5" or c["returned_model"] != "qwen3.5" for c in calls):
                raise ValueError(f"Unexpected generator UID in {arm}")
            if [c["prompt_sha256"] for c in calls] != [c["prompt_sha256"] for c in row["model_calls"]]:
                raise ValueError(f"Model receipt mismatch in {arm}, {row['sample_id']}")
            sample = {"ctx": fingerprint(material), "answer": prediction["response"], "citations": prediction["citations"],
                      "mode": t["answer_mode"], "draft": t.get("model_answer"), "repair": t.get("citation_repair"),
                      "backend": [{"raw": c["raw_answer"], "completion_tokens": (c.get("usage") or {}).get("completion_tokens"),
                                   "max_tokens": c.get("max_tokens"), "finish": c["finish_reason"]} for c in calls]}
            by_question[q["id"]].append((row, material, sample))
    for qid, samples in by_question.items():
        Random(f"formula-ingestion-ab-v1:{qid}").shuffle(samples)
        packet = {"question": questions[qid], "contexts": {}, "trials": {}}
        for i, (row, material, sample) in enumerate(samples, start=1):
            key = f"{qid}-{i}"
            packet["contexts"][sample["ctx"]] = material
            packet["trials"][key] = sample
            mapping[key] = {label: row[label] for label in ("arm", "id", "repeat", "sample_id")}
        dump(dest / f"{qid}.json", packet)
    dump(root / "blind-map.json", mapping)


def summarize(root, scorefiles):
    mapping = json.loads((root / "blind-map.json").read_text(encoding="utf-8"))
    scored = {}
    for path in scorefiles:
        for row in read_lines(path):
            key = row["key"]
            if key not in mapping or key in scored:
                raise ValueError(f"Unknown or duplicate score {key}")
            if any(type(row.get(k)) is not bool for k in ("semantic", "strict")) or (row["strict"] and not row["semantic"]):
                raise ValueError(f"Invalid grading flags for {key}")
            if not row.get("stage") or not row.get("reason"):
                raise ValueError(f"Missing diagnosis for {key}")
            scored[key] = {**row, **mapping[key]}
    if len(scored) != 36 or set(scored) != set(mapping):
        raise ValueError(f"Missing scores: {sorted(set(mapping) - set(scored))}")
    judged = sorted(scored.values(), key=lambda r: (r["arm"], r["id"], r["repeat"]))
    (root / "scores.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in judged), encoding="utf-8")
    summary = {"cases": list(CASES), "arms": {}, "paired": {}}
    for arm in ARMS:
        items = [r for r in judged if r["arm"] == arm]
        def counts(group):
            return {"n": len(group), "semantic": sum(r["semantic"] for r in group), "strict": sum(r["strict"] for r in group)}
        rows = read_arm(root, arm)
        receipts = [r for r in read_lines(root / f"model-calls-{arm}.jsonl") if r["success"]
                    and any(r["sample_id"] == x["sample_id"] and r["attempt"] == x["attempt"] for x in rows)]
        summary["arms"][arm] = {**counts(items), "cases": {qid: counts([x for x in items if x["id"] == qid]) for qid in CASES},
                                "median_api_seconds": median(r["http_seconds"] for r in rows),
                                "upstream_calls": len(receipts), "finish": dict(Counter(r["finish_reason"] for r in receipts)),
                                "at_cap": sum(isinstance((r["usage"] or {}).get("completion_tokens"),int)
                                              and r["usage"]["completion_tokens"] >= r["max_tokens"] for r in receipts)}
    for metric in ("semantic","strict"):
        lookup = {(r["sample_id"],r["arm"]): r[metric] for r in judged}
        outcome = Counter()
        for sample_id in {r["sample_id"] for r in judged}:
            b, a = (lookup[(sample_id,arm)] for arm in ARMS)
            outcome["both_pass" if b and a else "pass_to_fail" if b else "fail_to_pass" if a else "both_fail"] += 1
        summary["paired"][metric] = dict(outcome)
    dump(root / "summary.json", summary)
    print(json.dumps(summary["arms"],ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("packets", "summarize"))
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--scores", nargs="+", type=Path)
    args = parser.parse_args()
    if args.action == "packets":
        packets(args.root)
    elif args.scores:
        summarize(args.root, args.scores)
    else:
        parser.error("summarize requires --scores")
