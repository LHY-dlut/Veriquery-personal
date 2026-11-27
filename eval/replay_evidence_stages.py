"""Audit archived QA sessions against page-scoped, independently inspectable quotes."""
import argparse
from collections import Counter
import json
from pathlib import Path
import re
import unicodedata
from zipfile import ZipFile


def compact(text):
    # Preserve case: mΩ and MΩ denote different values.
    return "".join(unicodedata.normalize("NFC", text).split())


def numbered_context(trace):
    text = trace["context"]
    markers = list(re.finditer(r"(?m)^\[(\d+)\] ([^\n]+?) 第(\d+)页", text))
    evidence = trace["context_evidence"]
    if len(markers) != len(evidence):
        raise ValueError("Numbered sections do not match the archived evidence list")
    sections = []
    for i, (m, item) in enumerate(zip(markers, evidence)):
        if (int(m[1]), m[2], int(m[3])) != (i+1, item["file"], item["page"]):
            raise ValueError("Context citation identity differs from the evidence list")
        end = markers[i+1].start() if i+1 < len(markers) else len(text)
        sections.append({**item, "text": text[m.end():end]})
    return sections


def audit(bundle, gold, k):
    def lines(name):
        return [json.loads(line) for line in bundle.read(name).splitlines()]

    docs = {name: doc["document_id"] for name, doc in json.loads(bundle.read("docs.json")).items()}
    questions = {q["id"]:q for q in lines("questions.jsonl")}
    grades = json.loads(bundle.read("grades.json"))
    details = []
    for arm, path in [("before","before.jsonl"), ("after","candidate/after.jsonl")]:
        calls = lines(str(Path(path).with_name(f"model-calls-{arm}.jsonl")).replace("\\","/"))
        for row in lines(path):
            qid, session = row["id"], row["session_id"]
            answer = row["prediction"]
            trace = answer["trace"]
            required = gold["cases"][qid]
            scope = {docs[name] for name in questions[qid]["parts"]}

            def matches(candidate, key):
                fact = gold["facts"][key]
                if (candidate["document_id"], candidate["page"]) != (docs[fact["document"]], fact["page"]):
                    return False
                text = compact(candidate["text"])
                return all(compact(quote) in text for quote in fact["quotes"])

            ranked = trace["retrieved"][:k]
            context = numbered_context(trace)
            received = [c for c in calls if c["session_id"] == session]
            if received:
                if trace["context"] not in received[0]["messages"][0]["content"]:
                    raise ValueError(f"First model call differs from the trace in {session}")
            elif trace["answer_mode"] != "verified_dac_calculation":
                raise ValueError(f"No model receipt for {session}")
            mapping = {}
            for key in required:
                rank = next((i for i,c in enumerate(ranked,1) if matches(c,key)), None)
                refs = [c["chunk_id"] for c in context if matches(c,key)]
                mapping[key] = {"first_rank":rank, "context_chunks":refs}
            details.append(dict(arm=arm, question=qid, repeat=row["repeat"], session=session,
                                scope_ok=set(row["request"]["document_ids"]) == scope
                                and all(c["document_id"] in scope for c in trace["retrieved"]+context),
                                route=trace["answer_mode"], model_calls=len(received), facts=mapping,
                                strict=grades[arm][qid][row["repeat"]-1][0]))
    summary = {}
    for arm in ("before","after"):
        totals = Counter()
        for row in (r for r in details if r["arm"] == arm):
            facts = list(row["facts"].values())
            ranks = [f["first_rank"] for f in facts if f["first_rank"] is not None]
            shown = sum(bool(f["context_chunks"]) for f in facts)
            totals.update(sessions=1, scope=row["scope_ok"], strict=row["strict"], targets=len(facts),
                          hit_queries=bool(ranks), retrieved_targets=len(ranks), context_targets=shown,
                          complete_context_queries=shown==len(facts),
                          retained_targets=sum(bool(f["context_chunks"]) and f["first_rank"] is not None for f in facts))
        summary[arm]=dict(totals)
    return {"k":k, "summary":summary, "sessions":details}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("annotations", type=Path)
    parser.add_argument("--k", type=int, default=8)
    args = parser.parse_args()
    if args.k < 1:
        parser.error("k must be positive")
    with ZipFile(args.archive) as bundle:
        report = audit(bundle, json.loads(args.annotations.read_text(encoding="utf8")), args.k)
    print(json.dumps(report, ensure_ascii=False, indent=2))
