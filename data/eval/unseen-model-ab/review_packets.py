"""Group immutable traces by question, masking model and repeat IDs for manual review."""

import argparse
import hashlib
import json
from pathlib import Path
from random import Random


def lines(path):
    return [json.loads(s) for s in path.read_text("utf-8").splitlines() if s]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    opt = parser.parse_args()
    root = opt.root
    dest = root / "blind"
    dest.mkdir(exist_ok=True)
    questions = {}
    items = {}
    probes = {}
    for arm in ("current", "stronger"):
        rows = lines(root / f"{arm}.jsonl")
        taps = lines(root / f"model-calls-{arm}.jsonl")
        for row in rows:
            assert row["success"]
            q = row["question"]
            questions[q["id"]] = q
            pred = row["prediction"]
            trace = pred["trace"]
            calls = [x for x in taps if x["sample_id"] == row["sample_id"]
                     and x["attempt"] == row["attempt"] and x["success"]]
            context = trace.get("context") or ""
            evidence = trace.get("context_evidence") or []
            retrieved = [{"id": e["chunk_id"], "page": e["page"], "text": e["text"]}
                         for e in trace["retrieved"]]
            material = {"input": context, "context_evidence": evidence, "candidates": retrieved}
            ident = hashlib.sha256(json.dumps(material, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:12]
            sample = {"ctx": ident, "mode": trace["answer_mode"], "answer": pred["response"],
                      "citations": pred["citations"], "cleaned_draft": trace.get("model_answer"),
                      "repair": trace.get("citation_repair"),
                      "backend": [{"raw": x["raw_answer"], "finish": x["finish_reason"],
                                   "completion_tokens": (x["usage"] or {}).get("completion_tokens")} for x in calls]}
            items.setdefault(q["id"], []).append((arm, row["sample_id"], row["repeat"], ident, material, sample))
        for p in lines(root / f"arithmetic-probe-{arm}.jsonl"):
            probes.setdefault(p["id"], []).append((arm, p))
    mapping = {}
    for qid, question in sorted(questions.items()):
        packet = {"question": question, "contexts": {}, "trials": {}, "probes": {}}
        samples = items[qid]
        assert len(samples) == 6
        Random("fresh-devices-v1::"+qid).shuffle(samples)
        for i, (arm, sid, rep, ident, material, sample) in enumerate(samples, 1):
            key = f"{qid}-{i}"
            mapping[key] = {"id": qid, "arm": arm, "sample_id": sid, "repeat": rep}
            packet["contexts"][ident] = material
            packet["trials"][key] = sample
        diagnostic = probes.get(qid, [])
        Random("fresh-probe-v1::"+qid).shuffle(diagnostic)
        for i, (arm, p) in enumerate(diagnostic, 1):
            key = f"{qid}-probe-{i}"
            mapping[key] = {"id": qid, "arm": arm, "sample_id": "probe-"+qid}
            packet["probes"][key] = {"context": p["source_context"], "answer": p["cleaned"],
                                     "raw": p["raw"], "finish": p["finish_reason"]}
        (dest / f"{qid}.json").write_text(json.dumps(packet, ensure_ascii=False, indent=2), encoding="utf-8")
    (root / "blind-map.json").write_text(json.dumps(mapping, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
