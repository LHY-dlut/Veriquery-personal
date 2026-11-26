"""Validate and summarize the frozen, paired end-to-end model experiment."""

import argparse
from collections import Counter, defaultdict
import hashlib
import json
import math
from pathlib import Path
import re
from statistics import median


ARMS = ("current", "stronger")
PAIR_FIELDS = ("questions_sha256", "repeats", "documents", "index", "embedding", "reranker",
               "switches", "source_files")


def check(condition, detail):
    if not condition:
        raise ValueError(detail)


def read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def rows(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def unique(records, fields):
    result = {}
    for item in records:
        key = tuple(item[field] for field in fields)
        check(key not in result, f"Duplicate record: {key}")
        result[key] = item
    return result


def p95(values):
    values = sorted(values)
    rank = (len(values) - 1) * 0.95
    lo, hi = math.floor(rank), math.ceil(rank)
    return values[lo] + (values[hi] - values[lo]) * (rank - lo)


def validate_arm(root, arm, config, questions, expected):
    uid = config["model"]
    data = rows(root / f"{arm}.jsonl")
    keyed = unique(data, ("sample_id",))
    check(set(keyed) == {(key,) for key in expected}, f"{arm}: incomplete sample IDs")
    tap_records = rows(root / f"model-calls-{arm}.jsonl")
    tap = defaultdict(list)
    for call in tap_records:
        tap[(call.get("sample_id"), call.get("attempt"), call.get("session_id"))].append(call)
    responses, successful_calls, prompt_hashes, contexts, modes = {}, [], {}, {}, {}
    http_times, repairs, no_generation, downstream_failures = [], [], [], 0
    generated_attempt_calls = 0
    for sid, (case_id, repeat) in expected.items():
        record = keyed[(sid,)]
        check(record["id"] == case_id and record["repeat"] == repeat and record["arm"] == arm, sid)
        check(record["success"] and record["http_status"] == 200 and record["model"] == uid, f"{arm}:{sid} failed")
        question = questions[case_id]
        check(record["question"] == question, f"{arm}:{sid} question differs")
        body, prediction = record["request"], record["prediction"]
        doc_ids = [config["documents"][part]["id"] for part in question["parts"]]
        check(body["query"] == question["query"] and body["document_ids"] == doc_ids and body["trace"] is True, sid)
        check(prediction["success"] and prediction["session_id"] == record["session_id"] == body["session_id"], sid)
        trace = prediction["trace"]
        calls = tap[(sid, record["attempt"], record["session_id"])]
        generated_attempt_calls += len(calls)
        used = []
        for call in calls:
            check(call["arm"] == arm and call["case_id"] == case_id and call["repeat"] == repeat, f"Tap label: {sid}")
            check(call["requested_model"] == call["expected_model"] == uid, f"Tap route: {sid}")
            if call["success"]:
                check(call["returned_model"] == uid and re.fullmatch(r"[a-f0-9]{64}", call["prompt_sha256"]),
                      f"Tap response UID or prompt digest: {sid}")
                used.append(call)
            else:
                downstream_failures += 1
        check([c["prompt_sha256"] for c in used] == [c["prompt_sha256"] for c in record["model_calls"]],
              f"{arm}:{sid} successful tap receipt order differs")
        if not calls:
            check(trace.get("model_answer") is None and not any(c.get("draft") is not None for c in
                  trace.get("comparison_calls") or []), f"{arm}:{sid} generated but missing tap")
            no_generation.append(sid)
        if trace.get("citation_repair") is not None or any(c.get("citation_repair") is not None for c in
                                                           trace.get("comparison_calls") or []):
            repairs.append(sid)
        contexts[sid] = sha(trace["context"].encode()) if isinstance(trace.get("context"), str) else None
        # Use the first attempted prompt so a transport retry cannot mask an initial prompt mismatch.
        prompt_hashes[sid] = calls[0]["prompt_sha256"] if calls else None
        modes[sid] = trace["answer_mode"]
        http_times.append(record["http_seconds"])
        successful_calls.extend(used)
        responses[sid] = record
    counts = {}
    for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
        values = [(c["usage"] or {}).get(field) for c in successful_calls]
        known = [v for v in values if isinstance(v, int) and v >= 0]
        counts[field] = {"observed_calls": len(known), "total": sum(known) if known else None,
                         "median": median(known) if known else None}
    capped = [c for c in successful_calls if type((c["usage"] or {}).get("completion_tokens")) is int and
              isinstance(c.get("max_tokens"), int) and c["usage"]["completion_tokens"] >= c["max_tokens"]]
    stats = {
        "uid": uid, "backend_model": config["backend_model"], "successful_chat_requests": len(responses),
        "business_retries_before_scored_success": sum(r["attempt"] - 1 for r in responses.values()),
        "model_calls_succeeded_in_scored_attempts": len(successful_calls),
        "model_calls_failed_in_scored_attempts": downstream_failures,
        "tap_calls_outside_scored_attempts": len(tap_records) - generated_attempt_calls,
        "successful_model_calls_per_sample": dict(Counter(len(r["model_calls"]) for r in responses.values())),
        "answer_modes": dict(Counter(modes.values())), "no_model_call_samples": no_generation,
        "citation_repair_samples": repairs,
        "chat_seconds_p50": median(http_times), "chat_seconds_p95": round(p95(http_times), 4),
        "model_tokens_per_successful_call": counts,
        "finish_reasons": dict(Counter(c.get("finish_reason") or "unknown" for c in successful_calls)),
        "calls_with_completion_tokens_at_or_above_requested_cap": len(capped),
        "cap_samples": [c["sample_id"] for c in capped],
    }
    return stats, prompt_hashes, contexts


def paired_counts(ids, scores):
    result = {}
    for metric in ("semantic", "strict"):
        pairs = defaultdict(list)
        for sid in ids:
            a, b = (scores[(arm, sid)][metric] for arm in ARMS)
            change = ("both_pass" if b else "pass_to_fail") if a else ("fail_to_pass" if b else "both_fail")
            pairs[change].append(sid)
        current = len(pairs["both_pass"]) + len(pairs["pass_to_fail"])
        stronger = len(pairs["both_pass"]) + len(pairs["fail_to_pass"])
        result[metric] = {
            "denominator": len(ids), "current_pass": current, "stronger_pass": stronger,
            "net_change": stronger - current, "pair_counts": {k: len(pairs[k]) for k in
            ("both_pass", "fail_to_pass", "pass_to_fail", "both_fail")},
            "pair_sample_ids": dict(pairs),
        }
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path, help="Directory with both arms, tap logs and inventories")
    parser.add_argument("--questions", type=Path, help="Frozen questions.jsonl, defaults to root or its parent")
    parser.add_argument("--scores", type=Path, help="Adjudicated scores.jsonl (default: root/scores.jsonl)")
    args = parser.parse_args()
    root = args.root.resolve()
    qfile = args.questions or root / "questions.jsonl"
    if not args.questions and not qfile.exists():
        qfile = root.parent / "questions.jsonl"
    questions = {i[0]: q for i, q in unique(rows(qfile), ("id",)).items()}
    configs = {arm: read(root / f"{arm}-config.json") for arm in ARMS}
    current, stronger = (configs[arm] for arm in ARMS)
    for key in PAIR_FIELDS:
        check(current[key] == stronger[key], f"Arms differ in {key}")
    check(current["model"] != stronger["model"], "Arms use the same UID")
    check(sha(qfile.read_bytes()) == current["questions_sha256"], "Frozen questions hash mismatch")
    check(current["repeats"] == 3 and len(questions) == 12, "Expected 12 frozen questions and 3 repetitions")
    check(sha((root / "indexed.json").read_bytes()) == current["index"]["chroma_chunks_sha256"], "Index hash mismatch")
    inventory = read(root / "indexed.json")
    check(set(inventory) == set(current["documents"]), "Inventory documents mismatch")
    for part, item in current["documents"].items():
        check(len(inventory[part]) == item["chunks"], f"Inventory counts differ for {part}")
    expected = {f"{key}-r{repeat}": (key, repeat) for repeat in range(1, 4) for key in questions}
    scores = unique(rows(args.scores or root / "scores.jsonl"), ("arm", "sample_id"))
    check(set(scores) == {(arm, sid) for arm in ARMS for sid in expected}, "Expected 72 unique scored responses")
    for (arm, sid), r in scores.items():
        check((r["id"], r["repeat"]) == expected[sid], f"{arm}:{sid} score label mismatch")
        check(all(isinstance(r[k], (bool, int)) and r[k] in (0, 1) for k in ("semantic", "strict"))
              and not (r["strict"] and not r["semantic"]), f"{arm}:{sid} invalid binary grading")
        check("stage" in r and isinstance(r["reason"], str), f"{arm}:{sid} missing stage or reason")
    arms, prompts, contexts = {}, {}, {}
    for arm in ARMS:
        arms[arm], prompts[arm], contexts[arm] = validate_arm(root, arm, configs[arm], questions, expected)
        failed = [r for (a, _), r in scores.items() if a == arm and not r["strict"]]
        arms[arm]["strict_failure_stage_counts"] = dict(Counter(str(s) for r in failed for s in
            (r["stage"] if isinstance(r["stage"], list) else [r["stage"]])))
    groups = {
        "overall": list(expected),
        "answerable": [sid for sid, (i, _) in expected.items() if questions[i]["type"] != "unanswerable"],
        "arithmetic": [sid for sid, (i, _) in expected.items() if "calculation" in questions[i]["type"]],
        "unanswerable": [sid for sid, (i, _) in expected.items() if questions[i]["type"] == "unanswerable"],
        "cross_device": [sid for sid, (i, _) in expected.items() if len(questions[i]["parts"]) > 1],
    }
    alignment = {}
    for label, values in (("first_model_prompt", prompts), ("model_context", contexts)):
        by_pair = {}
        for sid in expected:
            a, b = (values[arm][sid] for arm in ARMS)
            by_pair[sid] = "neither" if a is None and b is None else "matched" if a == b else "mismatched"
        alignment[label] = {"pair_counts": dict(Counter(by_pair.values())), "by_pair": by_pair}
    groups["matched_first_model_prompt"] = [sid for sid, status in alignment["first_model_prompt"]["by_pair"].items()
                                            if status == "matched"]
    summary = {
        "planned_chat_requests": 72, "successful_scored_chat_requests": len(scores),
        "questions_sha256": current["questions_sha256"], "indexes": current["index"], "arms": arms,
        "paired_alignment": alignment, "groups": {k: paired_counts(v, scores) for k, v in groups.items()},
        "by_question": {i: paired_counts([s for s, (j, _) in expected.items() if i == j], scores) for i in questions},
        "definitions": {
            "unit": "One graded API response per model, question and repeat. Three repeats reuse each question.",
            "pair_direction": "current to stronger, paired by question and repeat label (no fixed random seed).",
            "p95": "Linear interpolation between adjacent sorted elapsed times at 0.95*(n-1).",
            "overlap": "Arithmetic includes comparison_calculation; cross_device can overlap arithmetic.",
            "finish_and_tokens": "Successful downstream chat completions from scored business attempts, including citation repair.",
            "cap": "Observed completion_tokens >= request.max_tokens, counted independently of server finish_reason.",
            "alignment": "'neither' means both sides lacked this input, distinct from matched nonempty receipt hashes.",
        },
    }
    destination = root / "summary.json"
    tmp = root / "summary.pending.json"
    tmp.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(destination)
    print(destination)


if __name__ == "__main__":
    main()
