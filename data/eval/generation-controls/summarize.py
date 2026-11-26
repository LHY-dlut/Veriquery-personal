"""Check the two-arm diagnostic and aggregate the frozen per-answer review."""

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import statistics
from tempfile import NamedTemporaryFile


CASES = ("h03", "h04", "h05", "h07")
CONTEXTS = {"original": "original_model_context", "short": "short_reviewed_context"}
SCORES = ("semantic", "strict", "raw_semantic", "raw_strict")
TOKEN_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens")
GENERATION = {"temperature": 0.1, "top_p": 0.9, "max_tokens": 256, "stream": False}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def load_jsonl(path):
    return [json.loads(line) for line in path.read_text("utf-8-sig").splitlines() if line.strip()]


def number_stats(values):
    if not values:
        return {"count": 0}
    return {"count": len(values), "sum": sum(values), "min": min(values), "median": statistics.median(values),
            "mean": round(statistics.mean(values), 3), "max": max(values)}


def aggregate(records):
    by_finish = {}
    for row in records:
        reason = row["finish_reason"] or "unknown"
        group = by_finish.setdefault(reason, {"n": 0, "semantic": 0, "strict": 0})
        group["n"] += 1
        group["semantic"] += row["review"]["semantic"]
        group["strict"] += row["review"]["strict"]
    return {
        "n": len(records),
        "passes": {field: sum(row["review"][field] for row in records) for field in SCORES},
        "finish_reasons": dict(Counter(row["finish_reason"] or "unknown" for row in records)),
        "scores_by_finish_reason": by_finish,
        "tokens": {
            field: number_stats([(row["usage"] or {})[field] for row in records
                                 if (row["usage"] or {}).get(field) is not None])
            for field in TOKEN_FIELDS
        },
        "elapsed_seconds": number_stats([row["elapsed_seconds"] for row in records]),
        "empty_raw_answer": sum(not row["raw_answer"].strip() for row in records),
        "empty_cleaned_answer": sum(not row["cleaned_answer"].strip() for row in records),
        "cleaning_changed_text": sum(row["raw_answer"] != row["cleaned_answer"] for row in records),
        "cleaning_changed_text_ids": [row["sample_id"] for row in records if row["raw_answer"] != row["cleaned_answer"]],
        "cleaning_changed_semantic": [
            row["sample_id"] for row in records if row["review"]["raw_semantic"] != row["review"]["semantic"]
        ],
        "cleaning_changed_strict": [
            row["sample_id"] for row in records if row["review"]["raw_strict"] != row["review"]["strict"]
        ],
    }


def read_arm(root, arm, controls, control_hash):
    generations = root / f"{arm}.jsonl"
    review_path = root / f"{arm}-scores.json"
    manifest_path = root / arm / "manifest.json"
    manifest = json.loads(manifest_path.read_text("utf-8-sig"))
    require(manifest["arm"] == arm and manifest["controls_sha256"] == control_hash,
            f"{arm}: manifest does not match the arm and frozen input")
    require(manifest["repetitions"] == 3 and manifest["generation"] == GENERATION,
            f"{arm}: manifest generation settings differ")
    rows = load_jsonl(generations)
    scores = json.loads(review_path.read_text("utf-8-sig"))
    by_id = {row["sample_id"]: row for row in rows}
    reviews = {row["sample_id"]: row for row in scores}
    expected = {f"{case}-{context}-r{rep}" for case in CASES for context in CONTEXTS for rep in range(1, 4)}
    require(len(rows) == len(scores) == 24 and set(by_id) == set(reviews) == expected,
            f"{arm}: expected exactly one result and one grade for all 24 cell IDs")

    prompt_hashes = {}
    for row in rows:
        key = row["sample_id"]
        case, context, rep = row["id"], row["context"], row["repeat"]
        require(key == f"{case}-{context}-r{rep}" and row["arm"] == arm and row["success"] is True
                and row["http_status"] == 200, f"{arm}/{key}: mismatched or failed trial")
        control = controls[case]
        evidence = control[CONTEXTS[context]]
        require(row["query"] == control["query"] and row["rubric"] == control["rubric"]
                and row["joint_comparison"] == control["joint_comparison"]
                and row["context_sha256"] == digest(evidence.encode()), f"{arm}/{key}: frozen inputs changed")
        request = row["request"]
        messages = request["messages"]
        require(len(messages) == 1 and messages[0]["role"] == "user", f"{arm}/{key}: unexpected messages")
        prompt = messages[0]["content"]
        require(digest(prompt.encode()) == row["prompt_sha256"] and evidence in prompt and control["query"] in prompt,
                f"{arm}/{key}: prompt hash, context, or question mismatch")
        require(all(request.get(k) == v for k, v in GENERATION.items())
                and request["model"] == manifest["requested_model"], f"{arm}/{key}: request settings mismatch")
        cell = (case, context)
        require(prompt_hashes.setdefault(cell, row["prompt_sha256"]) == row["prompt_sha256"],
                f"{arm}/{key}: prompts differ between repetitions")
        grade = reviews[key]
        require(all(isinstance(grade[k], bool) for k in SCORES)
                and (not grade["strict"] or grade["semantic"])
                and (not grade["raw_strict"] or grade["raw_semantic"]), f"{arm}/{key}: invalid binary score")
        for field in TOKEN_FIELDS:
            value = (row["usage"] or {}).get(field)
            require(value is None or (type(value) is int and value >= 0), f"{arm}/{key}: invalid {field}")
        require(row["finish_reason"] is None or isinstance(row["finish_reason"], str),
                f"{arm}/{key}: invalid finish reason")
        row["review"] = grade

    uids = {row["returned_model"] for row in rows}
    require(len(uids) == 1 and all(isinstance(uid, str) and uid for uid in uids), f"{arm}: ambiguous model UID")
    summary = {
        "requested_uid": manifest["requested_model"], "returned_uid": next(iter(uids)),
        "deployment": manifest["deployment"], "llm_client_sha256": manifest["llm_client_sha256"],
        "inputs_sha256": {p.name: digest(p.read_bytes()) for p in (generations, review_path, manifest_path)},
        "overall": aggregate(rows),
        "by_context": {ctx: aggregate([r for r in rows if r["context"] == ctx]) for ctx in CONTEXTS},
        "by_case": {},
    }
    for case in CASES:
        summary["by_case"][case] = {}
        for context in CONTEXTS:
            cell = sorted((r for r in rows if (r["id"], r["context"]) == (case, context)), key=lambda r: r["repeat"])
            summary["by_case"][case][context] = {
                **aggregate(cell), "prompt_sha256": prompt_hashes[(case, context)],
                "repetitions": [r["repeat"] for r in cell],
                "semantic_consistent": len({r["review"]["semantic"] for r in cell}) == 1,
                "strict_consistent": len({r["review"]["strict"] for r in cell}) == 1,
                "cleaned_answer_variants": len({r["cleaned_answer"] for r in cell}),
                "sample_ids": [r["sample_id"] for r in cell],
            }
    return summary, prompt_hashes


def main():
    home = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, default=home / "results")
    parser.add_argument("--controls", type=Path, default=home.parent / "context-coverage/release/generation-controls.jsonl")
    args = parser.parse_args()
    controls = load_jsonl(args.controls)
    require([c["id"] for c in controls] == list(CASES), "Unexpected frozen case list")
    controls = {c["id"]: c for c in controls}
    control_hash = digest(args.controls.read_bytes())
    current, prompts = read_arm(args.results, "current", controls, control_hash)
    stronger, compared_prompts = read_arm(args.results, "stronger", controls, control_hash)
    require(prompts == compared_prompts, "Cross-model prompts differ")
    require(current["returned_uid"] != stronger["returned_uid"], "Both arms reported the same model UID")
    require(current["llm_client_sha256"] == stronger["llm_client_sha256"], "LLMClient source hashes differ")
    summary = {
        "planned_trials": 48, "successful_scored_trials": 48, "controls_sha256": control_hash,
        "generation": GENERATION, "distinct_verified_prompts": len(set(prompts.values())),
        "arms": {"current": current, "stronger": stronger},
        "stronger_minus_current_passes": {
            ctx: {field: stronger["by_context"][ctx]["passes"][field] - current["by_context"][ctx]["passes"][field]
                  for field in SCORES} for ctx in CONTEXTS
        },
    }
    path = args.results / "summary.json"
    with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as file:
        json.dump(summary, file, ensure_ascii=False, indent=2)
        file.write("\n")
        temp = Path(file.name)
    temp.replace(path)
    print(path)


if __name__ == "__main__":
    main()
