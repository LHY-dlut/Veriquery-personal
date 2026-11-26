"""Verify lineage in the isolated six-session replay and emit compact, navigable receipts."""
from collections import defaultdict
import gzip
import hashlib
import json
from pathlib import Path
import sys


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def main():
    root=Path(sys.argv[1]); events=[json.loads(line) for line in gzip.decompress((root/"events.jsonl.gz").read_bytes()).splitlines()]
    result={}
    for path in sorted(root.glob("n*-ui.json")):
        ui=json.loads(path.read_text("utf8")); qid=ui["id"]; packet=ui["request"]
        end=next(m["data"] for m in packet["events"] if m["type"]=="complete")
        sid=end["session_id"]; trace=[e for e in events if e["session"]==sid]
        def one(stage):
            return next(e for e in trace if e["stage"]==stage)
        assert one("graph_entry")["arguments"]["query"]==packet["body"]["query"]
        assert one("graph_entry")["arguments"]["doc_ids"]==packet["body"]["document_ids"]
        assert end["response"]==ui["rendered_text"]==ui["ui_history_entry"]["content"]
        stages=[]
        for branch in dict.fromkeys(e["branch"] for e in trace if e["branch"]):
            es=[e for e in trace if e["branch"]==branch]
            by={e["stage"]:e for e in es}
            score=by["_rrf_fusion_input"]; source_scores=defaultdict(float)
            for source in set(h["source"] for h in score["hits"]):
                group=sorted((h for h in score["hits"] if h["source"]==source), key=lambda h:-h["score"])
                for rank,h in enumerate(group,1):
                    source_scores[h["doc"],h["id"]]+=score["weights"].get(source,.1)/(score["args"]["k"]+rank)
            fused=by["_rrf_fusion_output"]["hits"]
            for hit in fused:
                assert abs(source_scores[hit["doc"],hit["id"]]-hit["metadata"]["rrf_score"])<1e-10
            wire=next(e for e in es if e["stage"]=="backend_http" and e["path"].endswith("/rerank"))
            inputs=by["_rerank_candidates_input"]["hits"]
            sent=[("\n".join(filter(None,[h["metadata"].get("table_header"),h["metadata"].get("focused_text") or h["text"]]))
                   if h["metadata"].get("focused_row") else h["text"]) for h in inputs]
            assert sent==wire["request"]["documents"]
            scored={v["index"]:v["relevance_score"] for v in wire["reply"]["results"]}
            expected=[inputs[i]["id"] for i in sorted(scored,key=scored.get,reverse=True)]
            expected.extend(h["id"] for i,h in enumerate(inputs) if i not in scored)
            assert expected==[h["id"] for h in by["_rerank_candidates_output"]["hits"]]
            stages.append(dict(branch=branch,query=by["retrieve_input"]["args"],raw_counts={
                k:len(by[k+"_raw"]["rows"]) for k in ("ChromaDBVectorStore","BM25Store","TableStore")},
                rrf_k=score["args"]["k"],weights=score["weights"],
                ranks={k:[h["id"] for h in by[k]["hits"]] for k in (
                    "_vector_retrieve_output","retrieve_lexical_output","_table_retrieve_output","_rrf_fusion_output",
                    "_rerank_candidates_input","_rerank_candidates_output","retrieve_output")}))
        model=[e for e in trace if e["stage"]=="backend_http" and e["path"].endswith("/chat/completions")]
        assert all(e["reply"]["model"]==e["request"]["model"]=="qwen3.5" for e in model)
        assembled=one("assembled"); assert assembled["context"] in model[0]["request"]["messages"][0]["content"]
        detail={"session":sid,"case":qid,"entry":one("graph_entry")["arguments"],"retrieval":stages,
                "assembled_input_ids":assembled["input_ids"],"context":assembled["context"],"evidence":assembled["sources"],
                "model_calls":[{"messages":e["request"]["messages"],"raw_reply":e["reply"]} for e in model],
                "model_clean":[[e["raw"],e["cleaned"]] for e in trace if e["stage"]=="model_clean"],
                "graph_output":one("graph_output")["answer"],"api_complete":end,"ui_content_sha256":digest(ui["rendered_text"])}
        (root/(qid+"-chain.json")).write_text(json.dumps(detail,ensure_ascii=False,indent=2),encoding="utf8")
        result[qid]={"session":sid,"branches":len(stages),"llm_calls":len(model),"context_sources":len(assembled["sources"]),
                     "ui_matches_api":True,"rrf_scores_recomputed":True,"rerank_index_order_verified":True}
    assert len(result)==6 and sum(c["branches"] for c in result.values())==7
    (root/"verified.json").write_text(json.dumps(result,indent=2),encoding="utf8")


if __name__=="__main__":
    main()
