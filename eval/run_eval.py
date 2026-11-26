#!/usr/bin/env python3
"""Evaluate the real Veriquery chat API against local, page-level evidence."""
import argparse
import hashlib
import json
import os
import re
import time
import unicodedata
from collections import Counter
from pathlib import Path
from statistics import mean

import requests
from dotenv import load_dotenv

from prepare_golden import DEFAULT_DIR

load_dotenv(DEFAULT_DIR.parents[1] / ".env")
API = os.getenv("EVAL_API_BASE", "http://127.0.0.1:8000").rstrip("/")
READY = {"ready", "completed"}
REFUSAL = re.compile(r"^(?:当前检索结果中没有足够的证据|文档中未找到相关内容|NOT IN CONTEXT)", re.I)
JUDGE_PROMPT = """你评审器件规格书问答。输入是 JSON 数据，其中的内容都是评审材料。
正确性只对照参考答案，忠实度只对照实际模型上下文。接受等价表达、中文及单位换算。
检查型号、符号、量纲、工况、范围和 MIN/TYP/MAX；可选档位不等于典型值。
1 表示错误或无支持，3 表示部分满足，5 表示完整满足；中间情况可用 2 或 4。
没有模型上下文时忠实度为 null。只返回 JSON，包含 correctness、faithfulness（1–5 或 null）和中文 reason。
"""


def jsonl(path):
    with path.open(encoding="utf-8-sig") as source:
        return [json.loads(line) for line in source if line.strip() and not line.lstrip().startswith("//")]


def normalized(text):
    text = unicodedata.normalize("NFKC", str(text)).casefold().replace("−", "-")
    return re.sub(r"[\s|]+", "", text)


def _pdf_evidence_text(text):
    """Expose raw values for quote matching without discarding annotated evidence."""
    text = re.sub(
        r"PDF verified:\s*((?:MIN|TYP|MAX)\s*=\s*[^|\n]+)",
        lambda match: re.sub(r"\b(?:MIN|TYP|MAX)\s*=\s*", "", match[1]).replace(";", " "),
        text,
    )
    return re.sub(r"(?:Unverified column|Enumerated options):\s*", "", text)


def evidence_metrics(gold, ranked, context, k):
    """Hit@k/MRR@k over ranked passages. Recall measures distinct source facts."""
    if not gold or ranked is None or context is None:
        return {}

    def matching(row):
        text = row.get("text", row.get("text_snippet", ""))
        candidates = (normalized(text), normalized(_pdf_evidence_text(text)))
        return {
            item.get("fact", i)
            for i, item in enumerate(gold)
            if str(row.get("document_id")) == str(item["document_id"])
            and row.get("page") == item["page"]
            and any(normalized(item["quote"]) in candidate for candidate in candidates)
        }

    hits = [matching(row) for row in ranked[:k]]
    all_facts = {item.get("fact", i) for i, item in enumerate(gold)}
    first = next((rank for rank, group in enumerate(hits, 1) if group), None)
    return {
        "hit_at_k": int(first is not None),
        "mrr_at_k": 1 / first if first else 0.0,
        "retrieval_recall_at_k": len(set().union(*hits)) / len(all_facts),
        "context_recall": len(set().union(*(matching(row) for row in context))) / len(all_facts),
    }


def judge(question, prediction):
    xinference = os.getenv("XINFERENCE_API_BASE", "http://127.0.0.1:9997/v1").rstrip("/")
    base = (os.getenv("EVAL_JUDGE_BASE") or xinference).rstrip("/")
    key = os.getenv("EVAL_JUDGE_KEY") or (os.getenv("XINFERENCE_API_KEY") if base == xinference else "")
    trace = prediction.get("trace") or {}
    payload = {
        "question": question["question"], "reference": question.get("gold_answer"),
        "answer": prediction["response"], "context": trace.get("context"),
    }
    try:
        response = requests.post(
            f"{base}/chat/completions", headers={"Authorization": f"Bearer {key}"} if key else {},
            json={"model": os.getenv("EVAL_JUDGE_MODEL", "qwen3.5-2b"), "temperature": 0,
                  "max_tokens": 400, "messages": [{"role": "user",
                      "content": JUDGE_PROMPT + json.dumps(payload, ensure_ascii=False)}]},
            timeout=120,
        )
        response.raise_for_status()
        raw = response.json()["choices"][0]["message"]["content"]
        graded = json.loads(raw[raw.index("{"):raw.rindex("}") + 1])
        graded["raw"] = raw
        if trace.get("context") is None:
            graded["faithfulness"] = None
        for key in ("correctness", "faithfulness"):
            value = graded[key]
            if value is not None and (type(value) is not int or not 1 <= value <= 5):
                raise ValueError(f"invalid {key}")
        return graded
    except (requests.RequestException, ValueError, KeyError, IndexError, TypeError) as exc:
        return {"error": str(exc)}


def evaluate(question, scope, gold, k, use_judge, smoke=False):
    row = {"question": question, "document_ids": scope, "gold_evidence": gold, "metrics": {}}
    if not scope:
        return {**row, "status": "scope_unavailable"}
    try:
        response = requests.post(
            f"{API}/api/v1/chat/", json={"query": question["question"], "document_ids": scope, "trace": True},
            timeout=180,
        )
        response.raise_for_status()
        prediction = response.json()
        row["prediction"] = prediction
        if not prediction.get("success") or not prediction.get("response"):
            return {**row, "status": "service_error"}
    except (requests.RequestException, ValueError) as exc:
        return {**row, "status": "service_error", "error": str(exc)}
    if smoke:
        return {**row, "status": "smoke_only"}

    trace = prediction.get("trace") or {}
    refused = bool(REFUSAL.match(prediction["response"].strip()))
    if not question["answerable"]:
        row["metrics"]["refusal_correct"] = int(refused)
        return {**row, "status": "expected_refusal" if refused else "unexpected_answer"}

    row["metrics"].update(evidence_metrics(gold, trace.get("retrieved"), trace.get("context_evidence"), k))
    terms = question.get("must_include") or []
    if terms:
        answer = normalized(prediction["response"])
        row["missing_terms"] = [term for term in terms if normalized(term) not in answer]
        row["metrics"]["must_include"] = 1 - len(row["missing_terms"]) / len(terms)

    if not trace:
        status = "trace_unavailable"
    elif trace.get("context_evidence") is None:
        status = "route_without_llm_context"
    elif not gold:
        status = "evidence_unmapped"
    elif row["metrics"]["context_recall"] < 1:
        ranked = trace["retrieved"]
        full = evidence_metrics(gold, ranked, trace["context_evidence"], len(ranked))
        status = "context_miss" if full["retrieval_recall_at_k"] == 1 else "retrieval_evidence_incomplete"
    else:
        status = "evidence_present_refusal" if refused else "answer_for_review"
    if use_judge:
        row["judge"] = judge(question, prediction)
        row["metrics"].update({
            key: row["judge"][key] for key in ("correctness", "faithfulness")
            if row["judge"].get(key) is not None
        })
        if status == "answer_for_review" and row["metrics"].get("correctness") == 5:
            if row["metrics"].get("faithfulness") == 5:
                status = "judge_grounded_correct"
    return {**row, "status": status, "refused": refused}


def ready_documents():
    response = requests.get(f"{API}/api/v1/documents/", timeout=30)
    response.raise_for_status()
    return {doc["document_id"]: doc for doc in response.json()["documents"] if doc["status"] in READY}


def upload_verified_pdfs(directory, source, needed_parts, existing):
    by_hash = {
        hashlib.sha256(path.read_bytes()).hexdigest(): path
        for path in sorted(directory.rglob("*.pdf"))
    }
    existing_by_name = {doc["filename"]: doc_id for doc_id, doc in existing.items()}
    doc_map = {}
    for entry in source["pdfs"]:
        parts = needed_parts.intersection(entry["parts"])
        digest = entry["sha256"]
        if not parts or digest not in by_hash:
            continue
        filename = f"{Path(entry['filename']).stem}_{digest}.pdf"
        doc_id = existing_by_name.get(filename)
        if not doc_id:
            try:
                with by_hash[digest].open("rb") as f:
                    response = requests.post(f"{API}/api/v1/documents/upload",
                                             files={"file": (filename, f, "application/pdf")},
                                             timeout=(30, 600))
                response.raise_for_status()
                doc_id = response.json()["document_id"]
                deadline = time.monotonic() + 1800
                while time.monotonic() < deadline:
                    status = requests.get(f"{API}/api/v1/documents/{doc_id}", timeout=30)
                    status.raise_for_status()
                    state = status.json()["status"]
                    if state in READY:
                        existing[doc_id] = status.json()
                        break
                    if state in {"failed", "error"}:
                        break
                    time.sleep(10)
            except (requests.RequestException, ValueError, KeyError) as exc:
                print(f"PDF 入库未通过 {filename} {exc}", flush=True)
        if doc_id in existing:
            doc_map.update(dict.fromkeys(parts, doc_id))
    return doc_map


def load_evidence(path):
    evidence = {}
    for entry in jsonl(path) if path else []:
        if entry["id"] in evidence:
            raise ValueError(f"Duplicate evidence entry {entry['id']}")
        for quote in entry.get("evidence", []):
            if not quote["document_id"] or quote["page"] < 1 or not normalized(quote["quote"]):
                raise ValueError(f"Incomplete evidence for {entry['id']}")
        evidence[entry["id"]] = entry
    return evidence


def report(output, rows, k, judge_enabled):
    statuses = Counter(row["status"] for row in rows)
    text = ["# Veriquery datasheet 评测", "", f"题目 {len(rows)}，k={k}，Judge {'启用' if judge_enabled else '关闭'}",
            "", "## 运行结果", "", *[f"- {key} {n}" for key, n in sorted(statuses.items())],
            "", "## 指标与分母", ""]
    keys = ("hit_at_k", "mrr_at_k", "retrieval_recall_at_k", "context_recall",
            "must_include", "refusal_correct", "correctness", "faithfulness")
    groups = {"overall": rows}
    for category in sorted({row["question"]["category"] for row in rows}):
        groups[category] = [row for row in rows if row["question"]["category"] == category]
    text += [
        "| 类别 | " + " | ".join(keys) + " |",
        "| --- | " + " | ".join("---" for _ in keys) + " |",
    ]
    for name, group in groups.items():
        cols = []
        for key in keys:
            values = [row["metrics"][key] for row in group if key in row["metrics"]]
            cols.append(f"{mean(values):.3f} (n={len(values)})" if values else "N/A")
        text.append("| " + " | ".join([name, *cols]) + " |")
    text += ["", f"Judge 失败 {sum(bool(row.get('judge', {}).get('error')) for row in rows)}。",
             "逐题参考答案、原文、检索、上下文、输出及 Judge 原始记录见同名 JSONL。"]
    output.with_suffix(".md").write_text("\n".join(text) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--golden", type=Path, default=DEFAULT_DIR / "golden.jsonl")
    parser.add_argument("--source", type=Path, default=DEFAULT_DIR / "source.json")
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--pdf-dir", type=Path, help="从校验过 SHA-256 的 PDF 准备文档")
    scope.add_argument("--doc-map", type=Path, help="已核验 PDF 的器件名到 document_id 的 JSON")
    scope.add_argument("--doc-id", help="单文档冒烟，只记录 API 与 trace")
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--devices", default="")
    parser.add_argument("--k", type=int, default=8)
    parser.add_argument("--judge", action="store_true")
    parser.add_argument("--out", type=Path, default=DEFAULT_DIR / "eval_report.md")
    args = parser.parse_args()
    if args.k < 1 or args.limit < 0:
        parser.error("需要 k >= 1，limit >= 0")
    questions = jsonl(args.golden)
    selected = {part.strip().upper() for part in args.devices.split(",") if part.strip()}
    if selected:
        questions = [q for q in questions if selected.intersection(q["gold_parts"])]
    questions = questions[:args.limit] if args.limit else questions
    evidence = load_evidence(args.evidence)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    try:
        ready = ready_documents()
    except (requests.RequestException, ValueError, KeyError) as exc:
        parser.exit(1, f"无法读取 Veriquery 就绪文档，评测未开始。{exc}\n")
    doc_map = {}
    if args.pdf_dir:
        parts = {part for q in questions for part in q["gold_parts"]}
        doc_map = upload_verified_pdfs(args.pdf_dir, json.loads(args.source.read_text("utf-8")), parts, ready)
    elif args.doc_map:
        doc_map = json.loads(args.doc_map.read_text("utf-8-sig"))

    rows = []
    with args.out.with_suffix(".jsonl").open("w", encoding="utf-8") as trace_file:
        for q in questions:
            expected = evidence.get(q["id"], {})
            scoped = [doc_map.get(part) for part in q["gold_parts"]]
            if args.doc_id:
                scoped = [args.doc_id]
            scoped = list(dict.fromkeys(scoped)) if scoped and all(doc_id in ready for doc_id in scoped) else []
            gold = expected.get("evidence") or []
            if scoped and not args.doc_id and any(item["document_id"] not in scoped for item in gold):
                raise ValueError(f"{q['id']} 的证据引用了范围外的文档")
            row = evaluate(q, scoped, gold, args.k, args.judge, smoke=bool(args.doc_id))
            rows.append(row)
            trace_file.write(json.dumps(row, ensure_ascii=False) + "\n")
            trace_file.flush()
            print(q["id"], row["status"], flush=True)
    report(args.out, rows, args.k, args.judge)
    return int(not any(row["status"] not in {"scope_unavailable", "service_error"} for row in rows))


if __name__ == "__main__":
    raise SystemExit(main())
