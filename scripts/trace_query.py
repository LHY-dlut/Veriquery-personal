#!/usr/bin/env python3
"""逐节点打印一次问答的完整输入输出轨迹，用于定位断点。

不走 HTTP API，直接在 Python 层按 workflow_graph 的拓扑手动执行每个节点，
打印每一步"收到什么 → 调用谁 → 返回什么"。

用法（在 iSheet 目录下）:
    .venv/bin/python scripts/trace_query.py "What is the gain bandwidth of the OPA620?" --docs OPA620
    .venv/bin/python scripts/trace_query.py "OPA2993 的静态电流是多少" --docs a77ec99e
    .venv/bin/python scripts/trace_query.py "..." --docs a77ec99e --dump-records   # 额外打印该文档全部 param_record

需要服务所用模型在线（Xinference bge-m3 / qwen3.5 / rerank），但不依赖 uvicorn 进程。
"""
import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config import get_settings  # noqa: E402


def _short(text, n=400):
    text = (text or "").replace("\n", "\\n")
    return text[:n] + ("…" if len(text) > n else "")


def _show_chunk(tag, r, n=220):
    meta = getattr(r, "metadata", None) or {}
    keys = ["source", "page", "section", "param", "symbol", "conditions_raw",
            "pmin", "ptyp", "pmax", "punit", "raw_row", "schema_version"]
    meta_s = ", ".join(f"{k}={meta.get(k)}" for k in keys if k in meta)
    print(f"      [{tag}] page={getattr(r, 'page', '?')} score={getattr(r, 'score', 0):.3f} "
          f"doc={getattr(r, 'document_id', '?')} src={getattr(r, 'source', '?')}")
    print(f"        meta: {meta_s}")
    print(f"        text: {_short(getattr(r, 'text', ''), n)}")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("query")
    ap.add_argument("--docs", default="", help="逗号分隔的 document_id 列表（即 user_context.selected_document_ids）")
    ap.add_argument("--dump-records", action="store_true", help="打印选中文档的全部 param_record 元数据")
    args = ap.parse_args()

    settings = get_settings()
    doc_ids = [d.strip() for d in args.docs.split(",") if d.strip()]

    from agents.workflow_nodes import UnifiedAgentNodes
    from core import create_initial_state

    nodes = UnifiedAgentNodes(settings)

    # ── 环节 0: 意图识别 ────────────────────────────────────────────
    state = create_initial_state(query=args.query)
    state["session_id"] = "trace"
    state["user_context"] = {"selected_document_ids": doc_ids}
    state = await nodes.intent_router_node(state)
    print(f"\n[环节0 意图路由] query={_short(args.query, 120)}")
    print(f"  输入: query + user_context.selected_document_ids={doc_ids}")
    print(f"  输出: intent={state.get('intent')}（正则匹配，无模型调用）")

    if state.get("intent") == "pinout":
        state = await nodes.pinout_node(state)
        print("\n[环节1 pinout 节点]")
        print(f"  final_response: {_short(state.get('final_response'), 800)}")
        print(f"  extracted_data: {str(state.get('extracted_data'))[:800]}")
        print(f"  citations: {state.get('citations')}")
        return

    # ── 环节 1: 检索词改写 ──────────────────────────────────────────
    chip = nodes.text_nodes._extract_chip_name(args.query)
    print("\n[环节1 检索词改写]（text_retrieval_node 内部）")
    print(f"  _extract_chip_name -> {chip}")

    # ── 环节 2: 混合检索（向量+BM25+表格 → RRF → rerank）────────────
    print("\n[环节2 混合检索] retriever.retrieve(enhanced_query, top_k=30, ...)")
    from agents.workflow_nodes import TextAnalysisNodes  # noqa: F401  确保 _PARAM_KEYWORD_MAP 可见
    tn = nodes.text_nodes
    enhanced = args.query
    if chip:
        kws = [kw for kw in tn._extract_keywords(args.query) if kw != (chip or "").lower()]
        en_terms = []
        for zh, ens in tn._PARAM_KEYWORD_MAP.items():
            if zh in args.query:
                en_terms.extend(ens[:2])
        kws = kws + en_terms
        enhanced = f"{chip} {' '.join(kws[:4])}" if kws else f"{chip} supply voltage operating voltage"
    print(f"  enhanced_query = {enhanced}")
    print(f"  _is_param_query = {tn._is_param_query(args.query)}, "
          f"_is_cover_query = {tn._is_cover_query(args.query)}, "
          f"_is_transcription = {tn._is_transcription_request(args.query)}")

    filters = {"page": 1} if tn._is_cover_query(args.query) else None
    max_per_doc = 30 if (tn._is_param_query(args.query) or tn._is_cover_query(args.query)) else None
    results = await nodes.retriever.retrieve(enhanced, top_k=30, document_ids=doc_ids,
                                             filters=filters, max_per_doc=max_per_doc)
    print(f"  返回 {len(results)} 条（向量 top60 + BM25 top60 + 表格 → RRF 融合 → rerank 精排 top30）")
    for i, r in enumerate(results[:6]):
        _show_chunk(f"top{i+1}", r)

    if chip and results:
        kept = [r for r in results
                if chip.lower() in (getattr(r, "text", "") or "").lower()
                or chip.lower() in (getattr(r, "filename", "") or "").lower()]
        print(f"  型号过滤: {len(results)} -> {len(kept)}"
              + ("（过滤后为空则保留原始结果）" if not kept else ""))
        if kept:
            results = kept

    state["retrieval_results"] = results
    state["citations"] = tn._extract_citations(results)
    print(f"\n[环节2.1 citations]（取分数最高的 2 条，页面信息来自 chunk metadata）")
    for c in state["citations"]:
        print(f"  file={c.get('file')} page={c.get('page')} section={c.get('section')}")
        print(f"    snippet: {_short(c.get('text_snippet'), 200)}")

    # ── 环节 3: Resolver（参数题确定性选值）────────────────────────
    print("\n[环节3 Resolver] _try_param_resolution(query, results, doc_ids)")
    print(f"  _is_param_query 门控 = {tn._is_param_query(args.query)}")
    store = getattr(nodes.retriever, "vector_store", None)
    if args.dump_records and store is not None and doc_ids:
        where = {"$and": [{"document_id": {"$in": doc_ids}}, {"source": "param_record"}]}
        rows = store.fetch_by_metadata(where, limit=2000)
        print(f"  --dump-records: 该文档 param_record 全集 {len(rows)} 条")
        for i, row in enumerate(rows[:50]):
            m = row.get("metadata") or {}
            print(f"   #{i+1} param={m.get('param')} symbol={m.get('symbol')} "
                  f"conditions_raw={m.get('conditions_raw')} pmin={m.get('pmin')} "
                  f"ptyp={m.get('ptyp')} pmax={m.get('pmax')} punit={m.get('punit')} "
                  f"page={m.get('page')} sv={m.get('schema_version')}")
        if len(rows) > 50:
            print(f"   …（其余 {len(rows)-50} 条省略）")

    resolved, resolved_cites = await tn._try_param_resolution(args.query, results, doc_ids)
    print(f"  Resolver 输出: {None if resolved is None else _short(resolved, 600)}")
    if resolved:
        print("\n========== 最终答案（来自 Resolver，未经过 LLM）==========")
        print(resolved)
        if resolved_cites:
            print("\n[citations 回填]（用选值证据替代检索 top2）")
            for c in resolved_cites:
                print(f"  file={c.get('file')} page={c.get('page')} section={c.get('section')}")
                print(f"    snippet: {_short(c.get('text_snippet'), 200)}")
        return

    # ── 环节 4: LLM 生成（组装 context → _build_prompt → 调用 → 清洗）──
    print("\n[环节4 LLM 生成]（Resolver 返回 None，回退 RAG-LLM）")
    from core.llm_client import get_llm_client
    llm = get_llm_client(settings)

    orig_build = llm._build_prompt
    orig_generate = llm._generate
    orig_clean = llm._clean_answer

    def spy_build(query, context, transcription=False):
        prompt = orig_build(query, context, transcription)
        print(f"  --- _build_prompt 输出（最终 messages 的 user 内容，{len(prompt)} 字符）---")
        print(prompt)
        return prompt

    def spy_generate(prompt, max_new_tokens=256, temperature=0.3, transcription=False, **kw):
        raw = orig_generate(prompt, max_new_tokens=max_new_tokens, temperature=temperature,
                            transcription=transcription, **kw)
        print(f"  --- _generate 原始返回（清洗前，{len(raw or '')} 字符）---")
        print(_short(raw, 800))
        return raw

    def spy_clean(answer):
        cleaned = orig_clean(answer)
        print(f"  --- _clean_answer 清洗后（{len(cleaned or '')} 字符）---")
        print(_short(cleaned, 800))
        return cleaned

    llm._build_prompt = spy_build
    llm._generate = spy_generate
    llm._clean_answer = spy_clean

    answer = await tn._generate_text_response_optimized(args.query, results, doc_ids)

    print("\n========== 最终答案（LLM 路径）==========")
    print(answer)


if __name__ == "__main__":
    asyncio.run(main())
