#!/usr/bin/env python3
"""VeriQuery 端到端冒烟验收（部署到服务器后也用它）。

覆盖：
  1. 环境自检（Python 版本 / 关键依赖 / NO_PROXY）
  2. 服务健康（后端 /health、前端 8501）
  3. 主链路：上传 → 解析入库 → 文档状态
  4. 接口回归：文本问答（参数题 / 元信息题 / 拒答题）、电路检索、引脚、对比、ERC
  5. 回退链路：USE_MINERU_PARSE=false 重新入库，确认图像块不被丢弃
  6. 汇总 PASS/FAIL + JSON 报告

用法：
  .venv/bin/python scripts/e2e_smoke.py --pdf <某个PDF> [--api http://127.0.0.1:8000] [--ui http://127.0.0.1:8501]
退出码：0 = 全部关键项通过；1 = 有失败
"""
import argparse
import json
import os
import sys
import time
import uuid

# 以脚本所在目录的上一级（项目根）为 sys.path，保证能 import core / retrieval / ingestion
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
os.chdir(_PROJECT_ROOT)


import requests

PROXIES = {"http": None, "https": None}
RESULTS = []
PDF_PATH = None


def record(name, ok, detail=""):
    RESULTS.append({"name": name, "ok": bool(ok), "detail": str(detail)[:400]})
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""), flush=True)
    return ok


def api(method, path, **kw):
    kw.setdefault("timeout", 300)
    kw.setdefault("proxies", PROXIES)
    r = requests.request(method, f"{API}{path}", **kw)
    return r


# ---------------------------------------------------------------- 1. 环境自检
def check_env():
    print("\n== 1. 环境自检 ==")
    record("python >= 3.8", sys.version_info >= (3, 8), sys.version.split()[0])
    for mod in ["requests", "numpy", "fitz", "PIL", "torch", "streamlit", "langgraph"]:
        try:
            __import__(mod)
            record(f"import {mod}", True)
        except Exception as e:
            record(f"import {mod}", False, str(e)[:120])
    # chromadb 必须在 retrieval.vector_store 的 pysqlite3 替换之后 import，
    # 否则 RHEL8 的系统 sqlite 3.26 会触发 "requires sqlite3 >= 3.35.0"
    try:
        import retrieval.vector_store  # noqa: F401  触发 sqlite 替换
        import chromadb
        record("import chromadb（经 retrieval.vector_store 触发 sqlite 替换）", True,
               chromadb.__version__)
    except Exception as e:
        record("import chromadb（经 sqlite 替换）", False, str(e)[:160])
    # numpy / opencv 版本组合（chromadb 0.4.24 用 np.float_，NumPy2 会崩）
    try:
        import numpy
        record("numpy < 2.0（chromadb 0.4.24 需要）", numpy.__version__.startswith("1."),
               numpy.__version__)
    except Exception as e:
        record("numpy 版本检查", False, str(e)[:80])
    # sqlite 实际生效版本（替换后）
    try:
        import sqlite3
        record("sqlite3 >= 3.35（否则 chroma 报不支持）",
               sqlite3.sqlite_version_info >= (3, 35, 0), sqlite3.sqlite_version)
    except Exception as e:
        record("sqlite3 检查", False, str(e)[:80])


# ---------------------------------------------------------------- 2. 服务健康
def check_services():
    print("\n== 2. 服务健康 ==")
    try:
        r = requests.get(f"{API}/health", timeout=15, proxies=PROXIES)
        record("后端 /health", r.status_code == 200, r.text[:160])
    except Exception as e:
        record("后端 /health", False, str(e)[:160])
    try:
        r = requests.get(f"{UI}/_stcore/health", timeout=15, proxies=PROXIES)
        record("前端 8501 健康", r.status_code == 200, f"HTTP {r.status_code}")
    except Exception as e:
        record("前端 8501 健康", False, str(e)[:160])


# ---------------------------------------------------------------- 3. 上传入库
def upload_and_wait(timeout_sec=900):
    print("\n== 3. 上传与解析入库 ==")
    if not os.path.exists(PDF_PATH):
        record("PDF 文件存在", False, PDF_PATH)
        return None
    with open(PDF_PATH, "rb") as f:
        r = api("POST", "/api/v1/documents/upload",
                files={"file": (os.path.basename(PDF_PATH), f, "application/pdf")})
    if r.status_code != 200:
        record("上传", False, f"HTTP {r.status_code}: {r.text[:200]}")
        return None
    doc_id = r.json().get("document_id")
    record("上传", bool(doc_id), doc_id)
    if not doc_id:
        return None

    t0 = time.time()
    info = {}
    while time.time() - t0 < timeout_sec:
        try:
            info = api("GET", f"/api/v1/documents/{doc_id}").json()
        except Exception:
            time.sleep(5)
            continue
        st = info.get("status")
        if st in ("ready", "completed", "error", "failed"):
            break
        time.sleep(5)
    ok = info.get("status") in ("ready", "completed")
    record(f"入库完成（用时 {time.time()-t0:.0f}s）", ok,
           f"status={info.get('status')} chunks={info.get('chunk_count')} "
           f"tables={info.get('table_count')} images={info.get('image_count')}")
    record("文档有文本块", (info.get("chunk_count") or 0) > 0, info.get("chunk_count"))
    record("文档有表格", (info.get("table_count") or 0) > 0, info.get("table_count"))
    record("文档有图像块", (info.get("image_count") or 0) > 0, info.get("image_count"))
    return doc_id


# ---------------------------------------------------------------- 4. 接口回归
def check_endpoints(doc_id):
    print("\n== 4. 接口回归 ==")

    def chat(q, expect_ok=True):
        try:
            r = api("POST", "/api/v1/chat/", json={"query": q, "document_ids": [doc_id]})
            if r.status_code != 200:
                return None, f"HTTP {r.status_code}"
            d = r.json()
            return (d.get("response") or ""), (d.get("citations") or [])
        except Exception as e:
            return None, str(e)[:160]

    # 4.1 参数题：必须有答案且带引用
    ans, cites = chat("BSO301SP 的连续漏电流最大值是多少?")
    record("参数题有答案", bool(ans) and len(ans) > 5, (ans or "")[:90].replace("\n", " "))
    record("参数题带页码引用", len(cites) > 0, f"{len(cites)} 条")

    # 4.2 元信息题：答案必须真的答出内容，不能是"未找到"（否则是假通过）
    ans, _ = chat("首页标题是什么?")
    ok = bool(ans) and len(ans) > 3 and "未找到" not in ans
    record("元信息题答出内容（非拒答）", ok, (ans or "")[:90].replace("\n", " "))

    # 4.3 不存在的内容必须拒答（反幻觉）
    ans, _ = chat("订购/包装信息表逐行转录")
    hit = ans and ("未找到" in ans or "不包含" in ans)
    record("不存在内容被拒答（反幻觉）", bool(hit), (ans or "")[:90].replace("\n", " "))

    # 4.4 电路检索
    try:
        r = api("POST", "/api/v1/circuit/search", json={"query": "输出特性曲线", "top_k": 3})
        d = r.json()
        cs = d.get("circuits") or []
        record("电路检索返回结果", r.status_code == 200 and len(cs) > 0,
               f"命中 {len(cs)} 条，top1 page={cs[0].get('page') if cs else '-'}")
    except Exception as e:
        record("电路检索返回结果", False, str(e)[:160])

    # 4.5 引脚 / 对比 / ERC（走本地静态库，与文档无关）
    try:
        r = api("POST", "/api/v1/pinout/", json={"chip_name": "NE5532"})
        record("引脚查询", r.status_code == 200 and "pins" in r.text, f"HTTP {r.status_code}")
    except Exception as e:
        record("引脚查询", False, str(e)[:120])
    try:
        r = api("POST", "/api/v1/compare/devices-enhanced", json={"devices": ["NE5532", "LM358"]})
        record("器件对比", r.status_code == 200 and "devices" in r.text, f"HTTP {r.status_code}")
    except Exception as e:
        record("器件对比", False, str(e)[:120])
    try:
        r = api("POST", "/api/v1/erc/check", json={})
        record("ERC 检查可响应", r.status_code < 500, f"HTTP {r.status_code}")
    except Exception as e:
        record("ERC 检查可响应", False, str(e)[:120])


# ---------------------------------------------------------------- 5. 回退链路
def check_fallback():
    """fitz 回退链路：MinerU 不可用时图像块不应被丢弃。"""
    print("\n== 5. 回退链路（USE_MINERU_PARSE=false）==")
    if not os.path.exists(PDF_PATH):
        record("回退链路跳过（无 PDF）", False, PDF_PATH)
        return
    try:
        from core.config import get_settings
        from ingestion.document_processor import EnhancedDocumentProcessor
        import asyncio
        s = get_settings()
        s.USE_MINERU_PARSE = False
        proc = EnhancedDocumentProcessor(s)
        res = asyncio.run(proc.process_document(PDF_PATH))
        pages = res.get("processed_pages") or []
        n_img = sum(len(p.images) for p in pages)
        n_txt = sum(len(p.text or "") for p in pages)
        record("fitz 回退：页数>0", len(pages) > 0, f"{len(pages)} 页")
        record("fitz 回退：文本非空", n_txt > 0, f"{n_txt} 字符")
        # 关键回归：CLIP 分类异常时图像不应被丢
        record("fitz 回退：图像块未被丢弃", n_img > 0,
               f"{n_img} 个图像块（CLIP 异常时旧逻辑会丢成 0）")
    except Exception as e:
        record("fitz 回退链路", False, str(e)[:200])


def main():
    global API, UI, PDF_PATH
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", default="", help="测试 PDF 路径")
    ap.add_argument("--api", default="http://127.0.0.1:8000")
    ap.add_argument("--ui", default="http://127.0.0.1:8501")
    ap.add_argument("--out", default="", help="JSON 报告输出路径")
    args = ap.parse_args()
    API, UI = args.api.rstrip("/"), args.ui.rstrip("/")

    PDF_PATH = args.pdf
    if not PDF_PATH:
        # 回退：找 data/uploads 里已有的 PDF
        import glob
        cands = sorted(glob.glob("data/uploads/*.pdf"))
        PDF_PATH = cands[0] if cands else ""
    print(f"VeriQuery E2E 冒烟 | API={API} UI={UI} PDF={PDF_PATH}")

    check_env()
    check_services()
    doc_id = upload_and_wait()
    if doc_id:
        check_endpoints(doc_id)
    check_fallback()

    failed = [r for r in RESULTS if not r["ok"]]
    print("\n" + "=" * 60)
    print(f"总计 {len(RESULTS)} 项，通过 {len(RESULTS)-len(failed)}，失败 {len(failed)}")
    for r in failed:
        print(f"  FAIL: {r['name']} — {r['detail']}")
    print("=" * 60)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump({"api": API, "pdf": PDF_PATH, "results": RESULTS,
                       "passed": len(RESULTS) - len(failed), "failed": len(failed)},
                      f, ensure_ascii=False, indent=2)
        print(f"报告已写入 {args.out}")

    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
