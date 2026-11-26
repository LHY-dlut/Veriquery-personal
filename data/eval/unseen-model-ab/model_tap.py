"""Loopback-only recorder for the real chat app's non-streaming Xinference calls."""

import argparse
from datetime import datetime, timezone
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
from threading import Lock
import time
from urllib.parse import urlsplit


HOP_HEADERS = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
               "te", "trailer", "transfer-encoding", "upgrade"}


def forwarded(headers):
    excluded = HOP_HEADERS | {word.strip().lower() for word in headers.get("Connection", "").split(",")}
    return [(key, value) for key, value in headers.items() if key.lower() not in excluded]


class Tap(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, status, data, headers=()):
        self.send_response(status)
        for key, value in headers:
            if key.lower() not in {"content-length", "server", "date"}:
                self.send_header(key, value)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def record(self, row, start):
        row["elapsed_seconds"] = round(time.monotonic() - start, 4)
        with self.server.log_lock, self.server.output.open("a", encoding="utf-8") as out:
            out.write(json.dumps(row, ensure_ascii=False) + "\n")

    def error_reply(self, status, reason):
        self.reply(status, json.dumps({"error": {"message": reason}}).encode(),
                   [("Content-Type", "application/json")])

    def forward(self):
        url = urlsplit(self.path)
        if url.scheme or url.netloc or not self.path.startswith("/"):
            return self.error_reply(400, "Expected a relative origin-form path")
        if self.headers.get("Transfer-Encoding"):
            return self.error_reply(411, "Experiment requests require Content-Length")
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length < 0:
                raise ValueError()
        except ValueError:
            return self.error_reply(400, "Invalid Content-Length")
        body = self.rfile.read(length) if length else None
        row = None
        start = time.monotonic()
        chat = self.command == "POST" and url.path.rstrip("/") == "/v1/chat/completions"
        config = self.server.config
        if chat:
            try:
                request = json.loads(body or b"")
                messages = request["messages"]
                if not isinstance(messages, list) or not messages or not isinstance(request, dict):
                    raise ValueError()
            except (ValueError, TypeError, KeyError):
                return self.error_reply(400, "Expected JSON chat messages")
            if len(messages) == 1 and isinstance(messages[0], dict) and isinstance(messages[0].get("content"), str):
                prompt = messages[0]["content"]
            else:
                prompt = json.dumps(messages, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
            active = {}
            try:
                active = json.loads(self.server.active.read_text("utf-8"))
            except FileNotFoundError:
                pass
            except (OSError, ValueError):
                return self.error_reply(500, "Invalid experiment case marker")
            if not isinstance(active, dict):
                return self.error_reply(500, "Invalid experiment case marker")
            case_id = active.get("id", active.get("case_id"))
            row = {"id": case_id, "case_id": case_id, "arm": config.arm, "attempt": active.get("attempt"),
                   "sample_id": active.get("sample_id"), "repeat": active.get("repeat"),
                   "session_id": active.get("session_id"), "started_utc": datetime.now(timezone.utc).isoformat(),
                   "expected_model": config.expected_model, "requested_model": request.get("model"),
                   "returned_model": None, "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                   "raw_answer": None, "finish_reason": None, "usage": None, "success": False,
                   "max_tokens": request.get("max_tokens"), "temperature": request.get("temperature"),
                   "top_p": request.get("top_p")}
            if request.get("model") != config.expected_model or request.get("stream"):
                row.update(http_status=409, error="Unexpected model UID or streaming request")
                self.record(row, start)
                return self.error_reply(409, row["error"])

        conn = http.client.HTTPConnection(self.server.upstream.hostname, self.server.upstream.port, timeout=config.timeout)
        try:
            headers = {k: v for k, v in forwarded(self.headers)
                       if k.lower() != "host" and not (chat and k.lower() == "accept-encoding")}
            if chat:
                headers["Accept-Encoding"] = "identity"
            conn.request(self.command, self.path, body=body, headers=headers)
            upstream = conn.getresponse()
            status, reply_headers, data = upstream.status, forwarded(upstream.headers), upstream.read()
            if row is not None:
                row.update(http_status=status, upstream_status=status)
                if 200 <= status < 300:
                    result = json.loads(data)
                    choice = result["choices"][0]
                    message = choice["message"]
                    raw = message.get("content")
                    if isinstance(raw, list):
                        raw = "".join(x.get("text", "") for x in raw if isinstance(x, dict))
                    row.update(returned_model=result.get("model"), raw_answer=raw, usage=result.get("usage"),
                               finish_reason=choice.get("finish_reason"), response_id=result.get("id"))
                    if row["returned_model"] != config.expected_model:
                        row.update(http_status=502, error="Unexpected upstream completion model UID")
                        self.record(row, start)
                        return self.error_reply(502, row["error"])
                    row["success"] = True
                self.record(row, start)
        except (OSError, http.client.HTTPException, ValueError, KeyError, IndexError, TypeError) as exc:
            if row is not None:
                row.update(success=False, http_status=502, error=type(exc).__name__)
                self.record(row, start)
            return self.error_reply(502, "Failed to obtain verified upstream response")
        finally:
            conn.close()
        self.reply(status, data, reply_headers)

    do_GET = do_HEAD = do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = forward


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--arm", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--expected-model", required=True)
    parser.add_argument("--upstream", default="http://127.0.0.1:9997")
    parser.add_argument("--timeout", type=int, default=150)
    args = parser.parse_args()
    upstream = urlsplit(args.upstream)
    if (upstream.scheme != "http" or upstream.hostname not in {"127.0.0.1", "localhost", "::1"}
            or upstream.path not in {"", "/"} or upstream.username or upstream.password or upstream.query or upstream.fragment):
        parser.error("Expected an HTTP loopback upstream origin")
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", args.arm):
        parser.error("Invalid arm")
    args.root.mkdir(parents=True, exist_ok=True)
    with ThreadingHTTPServer(("127.0.0.1", args.port), Tap) as server:
        server.config, server.upstream, server.log_lock = args, upstream, Lock()
        server.output = args.root / f"model-calls-{args.arm}.jsonl"
        server.active = args.root / f"active-{args.arm}.json"
        server.serve_forever()


if __name__ == "__main__":
    main()
