"""A fake OpenAI-compatible model server that can be told to fail. No real model, no cost.

Serves POST /v1/chat/completions for two models:
  chaos-primary  behaves according to the current mode (below)
  anything else  (e.g. chaos-backup) answers normally, unless target=all
A normal answer is "Hello from <model>. You said: <last user message>", so you
can see which model answered and exactly what it received.

Modes for chaos-primary:
  ok    answer normally          429   rate limited            503  overloaded
  hang  never answer (timeout)   401   bad API key             400  bad request
  cut   streaming: send some text, then fail mid-stream (non-streaming: same as 503)

Set the mode at startup with CHAOS_MODE, or at runtime:
  POST /chaos?mode=503             switch mode (affects chaos-primary only)
  POST /chaos?mode=503&target=all  switch mode for every model: a full provider outage
  GET  /chaos                      current mode + how many requests chaos-primary received
"""

import json
import os
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

MODES = {"ok", "429", "503", "hang", "401", "400", "cut"}
PRIMARY = "chaos-primary"

state = {"mode": os.environ.get("CHAOS_MODE", "ok"), "target": "primary", "primary_requests": 0}
lock = threading.Lock()

ERRORS = {  # mode -> (status, OpenAI-style error type, message)
    "429": (429, "rate_limit_error", "Rate limit reached (chaos)"),
    "503": (503, "server_error", "The model is overloaded (chaos)"),
    "cut": (503, "server_error", "The model is overloaded (chaos)"),
    "401": (401, "invalid_request_error", "Incorrect API key provided (chaos)"),
    "400": (400, "invalid_request_error", "Invalid request (chaos)"),
}


def last_user_text(body: dict) -> str:
    """Echoed back in the reply, so you can see exactly what the model received (e.g. redacted PII)."""
    for message in reversed(body.get("messages", [])):
        if message.get("role") == "user":
            content = message.get("content")
            if isinstance(content, list):
                content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
            return str(content or "")[:200]
    return ""


def usage_for(body: dict) -> dict:
    """Believable token counts: ~4 characters per prompt token, up to 100 reply tokens."""
    prompt = max(1, len(json.dumps(body.get("messages", []))) // 4)
    completion = min(int(body.get("max_tokens") or 100), 100)
    return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion}


class Handler(BaseHTTPRequestHandler):
    def send_json(self, status: int, payload: dict, headers: dict | None = None):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def send_sse(self, payload: dict | str):
        line = payload if isinstance(payload, str) else json.dumps(payload)
        self.wfile.write(f"data: {line}\n\n".encode())
        self.wfile.flush()

    # --- control endpoint ---

    def do_GET(self):
        if urlparse(self.path).path == "/chaos":
            with lock:
                return self.send_json(200, dict(state))
        self.send_json(404, {"error": "not found"})

    def handle_control(self):
        query = parse_qs(urlparse(self.path).query)
        mode = query.get("mode", [""])[0]
        target = query.get("target", ["primary"])[0]
        if mode not in MODES or target not in ("primary", "all"):
            return self.send_json(400, {"error": f"mode must be one of {sorted(MODES)}; target primary or all"})
        with lock:
            state["mode"] = mode
            state["target"] = target
            state["primary_requests"] = 0
            snapshot = dict(state)
        print(f"chaos mode -> {mode} (target={target})", flush=True)
        self.send_json(200, snapshot)

    # --- the fake model ---

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/chaos":
            return self.handle_control()
        if path != "/v1/chat/completions":
            return self.send_json(404, {"error": "not found"})

        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        model = body.get("model", "")
        stream = bool(body.get("stream"))
        mode = "ok"
        with lock:
            if model == PRIMARY:
                state["primary_requests"] += 1
            if model == PRIMARY or state["target"] == "all":
                mode = state["mode"]

        if mode == "hang":
            time.sleep(600)  # the gateway's timeout fires long before this
            return
        if mode in ERRORS and not (mode == "cut" and stream):
            status, kind, message = ERRORS[mode]
            # LiteLLM uses a 429's Retry-After as the cooldown length, like a real provider's.
            headers = {"Retry-After": "10"} if status == 429 else None
            return self.send_json(status, {"error": {"message": message, "type": kind, "code": status}}, headers)

        said = last_user_text(body)
        reply = f"Hello from {model}." + (f" You said: {said}" if said else "")
        if not stream:
            return self.send_json(200, {
                "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": reply}, "finish_reason": "stop"}],
                "usage": usage_for(body),
            })

        # Streaming (Server-Sent Events), one word per chunk.
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        chunk_id, created = f"chatcmpl-{uuid.uuid4().hex[:12]}", int(time.time())

        def chunk(delta: dict, finish: str | None = None) -> dict:
            return {"id": chunk_id, "object": "chat.completion.chunk", "created": created, "model": model,
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}

        words = reply.split(" ")
        for i, word in enumerate(words):
            if mode == "cut" and i == 2:  # some text is already out, now fail
                self.send_sse({"error": {"message": "Connection to model lost mid-stream (chaos)", "type": "server_error"}})
                return
            self.send_sse(chunk({"role": "assistant", "content": word + " "} if i == 0 else {"content": word + " "}))
            time.sleep(0.05)
        final = chunk({}, "stop")
        if (body.get("stream_options") or {}).get("include_usage"):
            final["usage"] = usage_for(body)
        self.send_sse(final)
        self.send_sse("[DONE]")

    def log_message(self, *args):  # keep the output quiet
        pass


if __name__ == "__main__":
    if state["mode"] not in MODES:
        raise SystemExit(f"CHAOS_MODE must be one of {sorted(MODES)}")
    print(f"chaos LLM listening on :8080 (mode={state['mode']})", flush=True)
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
