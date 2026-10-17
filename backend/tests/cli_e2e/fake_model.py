"""An OpenAI-compatible model for the CLI's end-to-end suite: free, instant, and what it says is known (D294).

`python fake_model.py PORT` serves `POST /v1/chat/completions` until killed. Each answer is
`Done: <what was asked>`, with token counts, so a run's result and the workspace's usage are
both checkable; `GET /v1/models` lists `fake`. Standard library only: it runs beside a scratch backend, never
inside it.
"""
from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _text(content) -> str:
    if isinstance(content, list):
        return " ".join(str(p.get("text") or "") for p in content if isinstance(p, dict))
    return str(content or "")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:  # quiet: the suite reads the backend's log, not this
        pass

    def _send(self, status: int, body: dict) -> None:
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        if self.path.rstrip("/").endswith("/models"):
            return self._send(200, {"object": "list", "data": [{"id": "fake", "object": "model"}]})
        self._send(404, {"error": {"message": "not here"}})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        users = [m for m in body.get("messages") or [] if m.get("role") == "user"]
        asked = _text(users[-1].get("content")) if users else ""
        # What was asked: a chat turn's "User Request:" line, else the prompt's last line (a workflow step's opens
        # with its role and ends with its task).
        lines = [line.strip() for line in asked.splitlines() if line.strip()]
        request = next((line.split(":", 1)[1].strip() for line in lines if line.startswith("User Request:")),
                       lines[-1] if lines else "")
        reply = f"Done: {request[:80]}"
        call = self._tool_call(body, request)
        if call is not None and body.get("stream"):
            return self._stream(body.get("model") or "fake", "", {"prompt_tokens": 1, "completion_tokens": 1,
                                                                  "total_tokens": 2}, call)
        if call is not None:
            return self._send(200, {
                "id": "chatcmpl-fake", "object": "chat.completion", "model": body.get("model") or "fake",
                "choices": [{"index": 0, "finish_reason": "tool_calls",
                             "message": {"role": "assistant", "content": None, "tool_calls": [call]}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
        usage = {"prompt_tokens": max(1, len(asked) // 4), "completion_tokens": max(1, len(reply) // 4),
                 "total_tokens": max(2, (len(asked) + len(reply)) // 4)}
        if body.get("stream"):
            return self._stream(body.get("model") or "fake", reply, usage)
        self._send(200, {
            "id": "chatcmpl-fake", "object": "chat.completion", "model": body.get("model") or "fake",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": reply}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": max(1, len(asked) // 4), "completion_tokens": max(1, len(reply) // 4),
                      "total_tokens": max(2, (len(asked) + len(reply)) // 4)},
        })


    @staticmethod
    def _tool_call(body: dict, request: str):
        """Asked to "look up" something, with tools offered and none called yet: call one (a search, if there is
        one), its string arguments filled with what was asked. Then answer, as a model does."""
        tools = [t.get("function") or {} for t in body.get("tools") or []]
        messages = body.get("messages") or []
        called = any(m.get("role") == "tool" or m.get("tool_calls") or "call_fake_1" in json.dumps(m)
                     for m in messages)
        if not tools or called or "look up" not in request.lower():
            return None
        pick = next((t for t in tools if "search" in str(t.get("name"))), tools[0])
        params = (pick.get("parameters") or {}).get("properties") or {}
        required = (pick.get("parameters") or {}).get("required") or list(params)[:1]
        args = {k: request for k in required if (params.get(k) or {}).get("type", "string") == "string"}
        return {"id": "call_fake_1", "type": "function",
                "function": {"name": pick.get("name"), "arguments": json.dumps(args)}}

    def _stream(self, model: str, reply: str, usage: dict, call: dict | None = None) -> None:
        """The answer as OpenAI's SSE chunks: a word at a time, then the finish and the usage, then [DONE]."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        def chunk(delta: dict, finish=None, **extra) -> None:
            body = {"id": "chatcmpl-fake", "object": "chat.completion.chunk", "model": model,
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}], **extra}
            self.wfile.write(f"data: {json.dumps(body)}\n\n".encode())
            self.wfile.flush()

        if call is not None:
            chunk({"role": "assistant", "tool_calls": [{"index": 0, **call}]})
            chunk({}, "tool_calls", usage=usage)
        else:
            words = reply.split(" ")
            chunk({"role": "assistant", "content": words[0]})
            for word in words[1:]:
                chunk({"content": " " + word})
            chunk({}, "stop", usage=usage)
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
