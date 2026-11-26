#!/usr/bin/env python3
"""golden.jsonl 自动评测：iSheet 答题 → LLM Judge 判分 → 汇总报告。

数据流
------
    eval/golden.jsonl（题库，来自 datasheet-rag 项目，仅作题源）
        → 按 gold_parts 定位本地 PDF → 确保已上传到 iSheet（复用已入库文档）
        → POST /api/v1/chat/ 逐题提问
        → LLM Judge（OpenAI 兼容端点）按四档判分
        → 汇总 correct / hallucination / refused_correct / refused_wrong

用法
----
    # 全量（需要 --pdf-dir 指向存放各器件 datasheet 的目录）
    .venv/bin/python eval/run_eval.py --pdf-dir /path/to/pdfs

    # 冒烟：只跑前 N 题
    .venv/bin/python eval/run_eval.py --pdf-dir /path/to/pdfs --limit 10

    # 指定已有文档冒烟（不上传，用某个已入库 document_id 跑所有题）
    .venv/bin/python eval/run_eval.py --doc-id bb397d27 --limit 10

Judge 模型（环境变量可覆盖）
--------------------------
    EVAL_JUDGE_BASE  默认 http://127.0.0.1:9997/v1
    EVAL_JUDGE_MODEL 默认 qwen3.5-2b
    EVAL_JUDGE_KEY   Judge 与 Xinference 基址相同时默认沿用 XINFERENCE_API_KEY

⚠️ 注意：本地 judge 是小模型（0.8B/2B），判分结果**只作参考基线**，
   不是权威评审；如有更强的 OpenAI 兼容端点，用环境变量指过去。
"""
import argparse
import json
import os
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
os.chdir(_ROOT)


import requests  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

load_dotenv(_ROOT / ".env")

API = os.environ.get("EVAL_API_BASE", "http://127.0.0.1:8000").rstrip("/")
PROXIES = {"http": None, "https": None}

XINFERENCE_BASE = os.environ.get("XINFERENCE_API_BASE", "http://127.0.0.1:9997/v1").rstrip("/")
JUDGE_BASE = (os.environ.get("EVAL_JUDGE_BASE") or XINFERENCE_BASE).rstrip("/")
JUDGE_MODEL = os.environ.get("EVAL_JUDGE_MODEL", "qwen3.5-2b")
JUDGE_KEY = os.environ.get("EVAL_JUDGE_KEY", "")
if not JUDGE_KEY and JUDGE_BASE == XINFERENCE_BASE:
    JUDGE_KEY = os.environ.get("XINFERENCE_API_KEY", "")

JUDGE_HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
}
if JUDGE_KEY:
    JUDGE_HEADERS["Authorization"] = f"Bearer {JUDGE_KEY}"

JUDGE_PROMPT = """你是一个严谨的半导体datasheet问答评审器，负责判断模型回答和参考答案是否匹配。
规则：
1. 先看黄金样本标记 should_refuse：
   - 如果 should_refuse=true：代表文档中不存在该参数，模型应当拒绝给出具体数值，不能编造数值。只要模型给出了确定参数值，就算【幻觉错误】；如果模型说明文档无对应数据、拒绝回答，才算正确。
   - 如果 should_refuse=false：文档内存在该参数，需要对比数值、单位、工况条件。
2. 数值、单位必须对齐：
   - 允许等价换算（比如 1.2V = 1200mV）；
   - 负号、min/typ/max 边界不能错；
   - 工况条件（温度、电压、电流条件）必须匹配，脱离工况的数值视为错误。
3. 允许合理近似；但是不能把范围值、典型值混为一谈。
4. 歧义场景：文档存在多组不同工况参数，模型必须指出歧义，而不是任选一个数值直接输出，强行选值算幻觉。
5. 不要因为措辞不一样直接判错，重点看【事实、数值、单位、约束条件】。
6. 如果模型回答"未找到/无法核验"且 should_refuse=false，判 refused_wrong（漏答），
   但若模型同时给出了"请指明工况/条件对照"的歧义提示，判 correct 并在理由中注明"以歧义提示回应"。

输出格式，严格JSON，不要额外解释：
{{
  "judge_label": "correct | hallucination | refused_correct | refused_wrong",
  "reason": "一句话说明判定理由，控制在60字以内"
}}
字段释义：
- correct：参数、单位、工况全部匹配，回答正确
- hallucination：编造数值、选错工况、数值错误、忽略歧义强行输出
- refused_correct：应当拒绝，并且模型正确拒绝，没有给出数值
- refused_wrong：应当拒绝，模型却输出了具体数值（幻觉）

下面是待评审内容：
【Question】：{question}
【Expected Answer】：{expected_answer}
【Should Refuse】：{should_refuse}
【Model Output】：{pred_answer}
"""


# ---------------------------------------------------------------- iSheet API
def api_get_documents():
    r = requests.get(f"{API}/api/v1/documents/", timeout=30, proxies=PROXIES)
    r.raise_for_status()
    return (r.json() or {}).get("documents") or []


def _wait_ready(doc_id, timeout_sec=1200):
    """轮询文档直到 ready；返回最终 info。"""
    t0 = time.time()
    info = {}
    while time.time() - t0 < timeout_sec:
        try:
            info = requests.get(f"{API}/api/v1/documents/{doc_id}",
                                timeout=30, proxies=PROXIES).json()
        except Exception:
            time.sleep(10)
            continue
        if info.get("status") in ("ready", "completed", "error", "failed"):
            break
        time.sleep(10)
    return info


def api_upload(pdf_path, timeout_sec=1800):
    """上传并等待 ready。

    服务器是单 worker：前一文档在解析时，上传 POST 可能等待很久才响应，
    甚至 read timeout——但文档**可能已经创建**。因此超时后按文件名到
    documents 列表里恢复 document_id，而不是当作失败重传。
    """
    fname = os.path.basename(pdf_path)
    try:
        with open(pdf_path, "rb") as f:
            r = requests.post(f"{API}/api/v1/documents/upload",
                              files={"file": (fname, f, "application/pdf")},
                              timeout=(30, 600), proxies=PROXIES)
        r.raise_for_status()
        doc_id = r.json()["document_id"]
        info = _wait_ready(doc_id, timeout_sec)
        if info.get("status") in ("error", "failed"):
            raise RuntimeError(f"入库失败: {info}")
        return doc_id
    except requests.Timeout:
        # 超时恢复：按文件名找已创建的文档
        t0 = time.time()
        while time.time() - t0 < timeout_sec:
            for d in api_get_documents():
                if d.get("filename", "").lower() == fname.lower():
                    doc_id = d["document_id"]
                    info = _wait_ready(doc_id, timeout_sec)
                    if info.get("status") in ("error", "failed"):
                        raise RuntimeError(f"入库失败: {info}")
                    return doc_id
            time.sleep(10)
        raise TimeoutError(f"上传超时且未能恢复: {fname}")


def api_ask(question, doc_ids, timeout=180):
    try:
        r = requests.post(f"{API}/api/v1/chat/",
                          json={"query": question, "document_ids": doc_ids},
                          timeout=timeout, proxies=PROXIES)
        if r.status_code != 200:
            return f"[HTTP {r.status_code}]"
        return (r.json() or {}).get("response") or ""
    except Exception as e:
        return f"[异常] {e}"


# ---------------------------------------------------------------- PDF 定位
def scan_pdf_dir(pdf_dir, parts):
    """扫描目录，返回 {器件名: pdf路径}（按前两页文本匹配型号）。"""
    import fitz
    norm_parts = {re.sub(r"[^A-Za-z0-9]", "", p).upper(): p for p in parts}
    found = {}
    pdfs = sorted(Path(pdf_dir).rglob("*.pdf"))
    print(f"扫描 {len(pdfs)} 份 PDF，匹配 {len(norm_parts)} 个器件…")
    for path in pdfs:
        try:
            doc = fitz.open(str(path))
            text = "".join(doc[i].get_text() for i in range(min(2, len(doc))))
            doc.close()
        except Exception:
            continue
        norm = re.sub(r"[^A-Za-z0-9]", "", text.upper())
        for np_, part in norm_parts.items():
            if np_ in norm and part not in found:
                found[part] = str(path)
                print(f"  {part:14s} <- {path.name}")
    return found


def ensure_uploaded(part, pdf_path, existing):
    """existing: {filename_lower: doc_id}；按文件名包含器件名复用，否则上传。"""
    stem = Path(pdf_path).stem.lower()
    if stem in existing:
        return existing[stem], False
    for fname, doc_id in existing.items():
        if part.lower() in fname:
            return doc_id, False
    print(f"  上传 {part}: {pdf_path}")
    return api_upload(pdf_path), True


# ---------------------------------------------------------------- Judge
def judge_one(question, expected, should_refuse, pred):
    prompt = JUDGE_PROMPT.format(
        question=question, expected_answer=expected,
        should_refuse=bool(should_refuse), pred_answer=pred or "（空）",
    )
    payload = {
        "model": JUDGE_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "max_tokens": 200,
        "temperature": 0.0,
    }
    for attempt in range(2):
        try:
            r = requests.post(f"{JUDGE_BASE}/chat/completions",
                              headers=JUDGE_HEADERS, json=payload,
                              timeout=120, proxies=PROXIES)
            if r.status_code != 200:
                time.sleep(1)
                continue
            text = r.json()["choices"][0]["message"]["content"].strip()
            m = re.search(r"\{.*\}", text, re.DOTALL)
            if not m:
                continue
            data = json.loads(m.group())
            label = data.get("judge_label", "")
            if label in ("correct", "hallucination", "refused_correct", "refused_wrong"):
                return data
        except Exception:
            if attempt == 0:
                time.sleep(1)
    return {"judge_label": "judge_error", "reason": "judge 解析失败"}


# ---------------------------------------------------------------- 主流程
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--golden", default=str(_ROOT / "eval" / "golden.jsonl"))
    ap.add_argument("--pdf-dir", default="", help="各器件 datasheet 所在目录")
    ap.add_argument("--doc-id", default="", help="冒烟模式：用已有 document_id 跑全部题")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 题（0=全部）")
    ap.add_argument("--devices", default="", help="只跑指定器件（逗号分隔）")
    ap.add_argument("--out", default="eval/eval_report.md")
    args = ap.parse_args()

    items = [json.loads(l) for l in open(args.golden, encoding="utf-8") if l.strip()]
    if args.devices:
        want = {re.sub(r"[^A-Za-z0-9]", "", d).upper() for d in args.devices.split(",")}
        items = [i for i in items
                 if want & {re.sub(r"[^A-Za-z0-9]", "", p).upper()
                            for p in (i.get("gold_parts") or [])}]
    if args.limit:
        items = items[: args.limit]
    print(f"待评测题数: {len(items)}")

    doc_map = {}          # 器件 -> document_id
    if args.doc_id:
        for it in items:
            parts = it.get("gold_parts") or ["__all__"]
            for p in parts:
                doc_map.setdefault(p, args.doc_id)
    elif args.pdf_dir:
        parts = set()
        for it in items:
            parts.update(it.get("gold_parts") or [])
        found = scan_pdf_dir(args.pdf_dir, parts)
        existing = {d["filename"].lower(): d["document_id"] for d in api_get_documents()}
        by_pdf = {}                       # 同一份 PDF 只上传一次（家族 datasheet 会被多个器件名命中）
        for part, pdf in sorted(found.items(), key=lambda x: x[1]):
            if pdf in by_pdf:
                doc_map[part] = by_pdf[pdf]
                print(f"  {part:14s} 复用 {Path(pdf).name} -> {by_pdf[pdf]}")
                continue
            try:
                doc_id, uploaded = ensure_uploaded(part, pdf, existing)
                doc_map[part] = doc_id
                by_pdf[pdf] = doc_id
                if uploaded:
                    existing[Path(pdf).stem.lower()] = doc_id
            except Exception as e:
                print(f"  {part} 上传失败: {e}")
    else:
        print("⚠️ 未提供 --pdf-dir/--doc-id：无法定位器件文档，"
              "仅 answerable=false 的题可测拒绝行为。")

    # 复用/新上传的文档必须先到 ready，否则提问会拿空检索结果
    for uid in sorted(set(doc_map.values())):
        info = _wait_ready(uid, 1800)
        print(f"文档 {uid}: {info.get('status')} ({info.get('filename','')[:40]})")

    results = []
    skipped = 0
    for it in items:
        parts = it.get("gold_parts") or []
        doc_ids = [doc_map[p] for p in parts if p in doc_map]
        if not doc_ids and args.doc_id:
            doc_ids = [args.doc_id]
        if not doc_ids:
            skipped += 1
            results.append({**it, "pred_answer": "", "judge": {
                "judge_label": "skipped", "reason": "缺少对应器件 PDF"}})
            continue
        pred = api_ask(it["question"], doc_ids)
        jd = judge_one(it["question"], it.get("gold_answer", ""),
                       not it.get("answerable", True), pred)
        results.append({**it, "pred_answer": pred, "judge": jd})
        print(f"[{it['id']}] {jd['judge_label']:16s} {jd.get('reason','')[:60]}"
              f"  | 答: {pred[:60].replace(chr(10),' ')}")

    # 汇总
    counts = defaultdict(int)
    for r in results:
        counts[r["judge"]["judge_label"]] += 1
    total = len(results)
    judged = total - counts["skipped"] - counts["judge_error"]
    md = ["# golden.jsonl 评测报告", "",
          f"- 题数：{total}（跳过 {counts['skipped']}，judge 失败 {counts['judge_error']}）",
          f"- Judge 模型：`{JUDGE_MODEL}`（本地小模型，结果作参考基线）", "",
          "| 指标 | 值 |", "|---|---|"]
    if judged:
        md.append(f"| 正确率 correct | {counts['correct']}/{judged} = {counts['correct']/judged:.1%} |")
        md.append(f"| 幻觉率 hallucination | {counts['hallucination']}/{judged} = {counts['hallucination']/judged:.1%} |")
        md.append(f"| 拒绝校准 refused_correct | {counts['refused_correct']}/{judged} |")
        md.append(f"| 漏答 refused_wrong | {counts['refused_wrong']}/{judged} |")
    md += ["", "## 逐题明细", "", "| id | 器件 | 判定 | 理由 | 模型回答(截断) |",
           "|---|---|---|---|---|"]
    for r in results:
        parts = ",".join(r.get("gold_parts") or [])
        ans = (r.get("pred_answer") or "").replace("|", "/")[:80].replace("\n", " ")
        md.append(f"| {r['id']} | {parts} | {r['judge']['judge_label']} | "
                  f"{r['judge'].get('reason','')[:50]} | {ans} |")

    Path(args.out).write_text("\n".join(md), encoding="utf-8")
    out_json = Path(args.out).with_suffix(".json")
    out_json.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告: {args.out} / {out_json}")
    print(f"汇总: {dict(counts)}")


if __name__ == "__main__":
    main()
