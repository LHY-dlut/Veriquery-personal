"""Exercise document selection, submission, SSE collection and the rendered chat bubble."""
import hashlib
import html
import json
import os
from pathlib import Path
import re
import sys

import requests
from streamlit.testing.v1 import AppTest


def main():
    root=Path(sys.argv[1]).resolve()
    ids=sys.argv[2:]
    os.environ["API_URL"]="http://127.0.0.1:8221"
    ui=Path.cwd()/"ui"
    sys.path.insert(0,str(ui))
    docs=json.loads((root/"documents.json").read_text())
    cases={q["id"]:q for q in map(json.loads,(root/"questions.jsonl").read_text().splitlines())}
    exchange=[]
    send=requests.Session.send
    def capture(self, req, **kwargs):
        resp=send(self,req,**kwargs)
        if req.method=="POST" and req.url.endswith("/api/v1/chat/stream"):
            receipt={"body":json.loads(req.body),"route":req.url,"http_status":resp.status_code,"events":[]}
            exchange.append(receipt)
            raw_lines=resp.iter_lines
            def lines(*a,**k):
                for line in raw_lines(*a,**k):
                    if line:
                        s=line.decode("utf8").removeprefix("data: ")
                        receipt["events"].append(json.loads(s))
                    yield line
            resp.iter_lines=lines
        return resp
    requests.Session.send=capture
    app=AppTest.from_file(str(ui/"app.py"),default_timeout=150)
    app.run()
    app.switch_page("pages/2_Chat.py").run()
    for qid in ids:
        q=cases[qid]
        wanted=[docs[name]["document_id"] for name in q["parts"]]
        app.multiselect(key="sidebar_doc_multiselect").set_value(wanted).run()
        app.text_input(key="chat_question_input").set_value(q["query"]).run()
        pos=len(exchange)
        app.button(key="send_btn").click().run()
        assert not list(app.exception),list(app.exception)
        assert len(exchange)==pos+1,(qid,exchange[pos:])
        receipt=exchange[-1]
        complete=next(e["data"] for e in receipt["events"] if e["type"]=="complete")
        entry=app.session_state["chat_history"][-1]
        bubble=next(m.value for m in reversed(list(app.markdown)) if 'class="chat-message chat-message-assistant"' in m.value)
        captured=re.search(r'<div class="chat-text">(.*?)</div>',bubble,re.S)[1]
        visible=html.unescape(captured.replace("<br>","\n"))
        chunks="".join(e["data"]["chunk"] for e in receipt["events"] if e["type"]=="chunk")
        assert receipt["body"]["query"]==q["query"] and receipt["body"]["document_ids"]==wanted
        assert chunks==complete["response"]==entry["content"]==visible and complete["citations"]==entry["citations"]
        result={"id":qid,"selected":wanted,"request":receipt,"ui_history_entry":entry,"rendered_html":bubble,
                "rendered_text":visible,"schema":requests.get(os.environ["API_URL"]+"/openapi.json",timeout=10).json()["components"]["schemas"]["ChatRequest"]}
        (root/(qid+"-ui.json")).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf8")
        print(qid,complete["session_id"],len(chunks),hashlib.sha256(visible.encode()).hexdigest(),flush=True)


if __name__=="__main__":
    main()
