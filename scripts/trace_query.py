#!/usr/bin/env python3
"""Trace the deployed chat graph from retrieval candidates to cited evidence."""
import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.dependencies import get_service_container
from core import create_initial_state


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("query")
    parser.add_argument("--docs", required=True, help="逗号分隔的 document_id")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("retrieval.hybrid_retriever").setLevel(logging.DEBUG)
    doc_ids = [doc_id.strip() for doc_id in args.docs.split(",") if doc_id.strip()]
    if not doc_ids:
        parser.error("--docs 至少需要一个 document_id")

    nodes = get_service_container().veriquery_graph.agent_nodes
    state = create_initial_state(query=args.query)
    state["user_context"] = {"selected_document_ids": doc_ids, "evaluation_trace": True}
    state = await nodes.intent_router_node(state)
    print(f"问题 {args.query}\n文档 {doc_ids}\n意图 {state['intent']}", flush=True)

    if state["intent"] == "pinout" and nodes._extract_chip_name(args.query):
        state = await nodes.pinout_node(state)
    else:
        state = await nodes.text_retrieval_node(state)
        print("\n最终检索排序")
        for rank, chunk in enumerate(state["retrieval_results"], 1):
            print(f"{rank:2} {chunk.chunk_id} {chunk.filename} p{chunk.page} "
                  f"{chunk.score:.4f} {chunk.metadata.get('fusion_sources')}")
        state = await nodes.response_generation_node(state)
        print(f"\n模型上下文\n{state.get('evaluation_context') or '（无）'}", flush=True)

    print(f"\n答案\n{state.get('final_response')}\n\n引用\n{state.get('citations')}")
    if state.get("error"):
        print(state["error"], file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
