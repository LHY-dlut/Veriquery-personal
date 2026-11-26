"""Evaluate the already-rendered answers using the project's optional judge implementation."""
import json
import gzip
import sys
from pathlib import Path

import requests

root=Path(sys.argv[1]).resolve()
sys.path.insert(0, str(Path.cwd()/"eval"))
import run_eval

events=list(map(json.loads,gzip.decompress((root/"events.jsonl.gz").read_bytes()).decode().splitlines()))
gold={q["id"]:q for q in map(json.loads,(root/"questions.jsonl").read_text().splitlines())}
calls=[]
original=requests.Session.send
def record(self,req,**kw):
    response=original(self,req,**kw)
    calls.append({"request":json.loads(req.body),"http":response.status_code,"reply":response.json()})
    return response
requests.Session.send=record
with (root/"judge.jsonl").open("w",encoding="utf8") as out:
    for path in sorted(root.glob("n*-ui.json")):
        u=json.loads(path.read_text())
        sid=u["request"]["events"][-1]["data"]["session_id"]
        context=next(e["context"] for e in events if e["session"]==sid and e["stage"]=="assembled")
        q=gold[u["id"]]; calls.clear()
        verdict=run_eval.judge({"question":q["query"],"gold_answer":q["gold"]},
                               {"response":u["rendered_text"],"trace":{"context":context}})
        out.write(json.dumps({"id":q["id"],"session":sid,"gold":q,"calls":calls,
                              "verdict":verdict},ensure_ascii=False)+"\n")
        out.flush()
