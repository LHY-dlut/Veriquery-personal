"""Observation-only hooks for a loopback replay of the production graph."""
import contextvars
import dataclasses
import functools
import inspect
import json
import sys
from pathlib import Path
from threading import Lock
from urllib.parse import urlsplit
from uuid import uuid4

import requests
import uvicorn


session = contextvars.ContextVar("session", default="")
branch = contextvars.ContextVar("retrieval", default="")
lock = Lock()


def log(stage, **data):
    if session.get():
        with lock, output.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"session": session.get(), "branch": branch.get(), "stage": stage,
                                     **data}, ensure_ascii=False, default=str) + "\n")


def hits(rows):
    return [{"id": x.chunk_id, "doc": x.document_id, "page": x.page, "source": x.source,
             "text": x.text, "score": x.score, "metadata": x.metadata} for x in rows]


def instrument():
    from api.routers import chat
    from agents.workflow_nodes import TextAnalysisNodes
    from retrieval.hybrid_retriever import HybridRetriever
    from core.llm_client import LLMClient
    from retrieval.bm25_store import BM25Store
    from retrieval.table_store import TableStore
    from retrieval.vector_store import ChromaDBVectorStore

    def search_store(cls):
        old=cls.search
        def record(obj,args,kwargs,result):
            bound=inspect.signature(old).bind(obj,*args,**kwargs)
            bound.apply_defaults()
            params={k:v for k,v in bound.arguments.items() if k not in ("self","query_embedding","filter_func")}
            if "query_embedding" in bound.arguments:
                params["query_dimension"]=len(bound.arguments["query_embedding"])
            if "filter_func" in bound.arguments:
                params["filter_applied"]=bound.arguments["filter_func"] is not None
            log(cls.__name__+"_raw",args=params,
                rows=[dataclasses.asdict(x) if dataclasses.is_dataclass(x) else x for x in result])
            return result
        if inspect.iscoroutinefunction(old):
            @functools.wraps(old)
            async def wrapper(obj,*args,**kwargs):
                return record(obj,args,kwargs,await old(obj,*args,**kwargs))
        else:
            @functools.wraps(old)
            def wrapper(obj,*args,**kwargs):
                return record(obj,args,kwargs,old(obj,*args,**kwargs))
        cls.search=wrapper
    for store in (BM25Store,TableStore,ChromaDBVectorStore):
        search_store(store)

    execute = chat._execute_agent_workflow
    @functools.wraps(execute)
    async def workflow(*args, **kwargs):
        bound = inspect.signature(execute).bind(*args, **kwargs).arguments
        session.set(bound["session_id"])
        log("graph_entry", arguments={k: v for k, v in bound.items() if k != "container"})
        return await execute(*args, **kwargs)
    chat._execute_agent_workflow = workflow

    def async_method(name):
        old = getattr(HybridRetriever, name)
        @functools.wraps(old)
        async def observed(self, *args, **kwargs):
            token = branch.set(uuid4().hex[:12]) if name == "retrieve" else None
            try:
                values = inspect.signature(old).bind(self, *args, **kwargs)
                values.apply_defaults()
                log(name + "_input", args={k:v for k,v in values.arguments.items() if k!="self"})
                result = await old(self, *args, **kwargs)
                log(name + "_output", hits=hits(result))
                return result
            finally:
                if token is not None:
                    branch.reset(token)
        setattr(HybridRetriever, name, observed)

    for name in ("retrieve", "_vector_retrieve", "retrieve_lexical", "_table_retrieve"):
        async_method(name)

    def ranked_method(name, key):
        old = getattr(HybridRetriever, name)
        @functools.wraps(old)
        def observed(self, *args, **kwargs):
            values = inspect.signature(old).bind(self, *args, **kwargs)
            values.apply_defaults()
            log(name + "_input", hits=hits(values.arguments[key]), args={
                k:v for k,v in values.arguments.items() if k not in ("self", key)},
                weights=self.source_weights if name=="_rrf_fusion" else None)
            result = old(self, *args, **kwargs)
            log(name + "_output", hits=hits(result))
            return result
        setattr(HybridRetriever, name, observed)

    for name, key in (("_rrf_fusion","results"),("_rerank_candidates","candidates"),("_limit_results","ranked")):
        ranked_method(name, key)

    build = TextAnalysisNodes._build_evidence_context
    @functools.wraps(build)
    def context(self, query, results, *args, **kwargs):
        rendered, sources = build(self, query, results, *args, **kwargs)
        log("assembled", query=query, input_ids=[x.chunk_id for x in results], context=rendered, sources=sources)
        return rendered, sources
    TextAnalysisNodes._build_evidence_context = context

    send = requests.Session.send
    @functools.wraps(send)
    def observed_send(self, req, **kwargs):
        u = urlsplit(req.url)
        response = send(self, req, **kwargs)
        if session.get() and u.port == 9997 and u.hostname in ("localhost","127.0.0.1") and (
                u.path.endswith("/rerank") or u.path.endswith("/chat/completions")):
            log("backend_http", path=u.path, request=json.loads(req.body), status=response.status_code,
                reply=response.json() if response.ok else response.text[:200])
        return response
    requests.Session.send = observed_send

    clean = LLMClient._clean_answer
    @functools.wraps(clean)
    def cleanup(self, answer):
        result=clean(self, answer)
        log("model_clean", raw=answer, cleaned=result)
        return result
    LLMClient._clean_answer=cleanup

    extract=chat._extract_results_from_state
    @functools.wraps(extract)
    def graph_result(state):
        log("graph_output", answer=state.get("final_response"), citations=state.get("citations"),
            results=hits(state.get("retrieval_results") or []), query=state.get("query"))
        return extract(state)
    chat._extract_results_from_state=graph_result
    def wrap_clean(name):
        old=getattr(chat,name)
        @functools.wraps(old)
        def wrapped(text):
            result=old(text)
            log(name,input=text,output=result)
            return result
        setattr(chat,name,wrapped)
    for name in ("_clean_role_markers","_deduplicate_sentences","_validate_response_quality"):
        wrap_clean(name)


if __name__ == "__main__":
    output=Path(sys.argv[1]).resolve()
    sys.path.insert(0,str(Path.cwd()))
    from api.main import app
    instrument()
    uvicorn.run(app,host="127.0.0.1",port=int(sys.argv[2]))
