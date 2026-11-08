"""The tool bridge: an agent's tools offered to a model CLI as real tools, run by the engine (F753).

Run with no tools of its own, newer Codex models would not use the tools written
into the prompt as text — "the order tools aren't available in this chat":
gpt-6-luna emitted the engine's call 0 times in 5 (CLIM1, 2026-10-17). Given real
tools, they call them.

This is a stdio MCP server the CLI starts for one call. It offers exactly the
agent's tools and runs none of them: a call is recorded and the CLI is told to
stop, and Synapse hands the recorded calls to the engine as the model's tool calls
— so the engine's tool policy, approvals, guards, memory and boards apply as they
do for any model. The result comes back to the CLI on its next turn.

Standard library only: the CLI starts it per call, outside Synapse's process.

    python cli_bridge_server.py <tools.json> <calls.jsonl>

`tools.json` is a list of {"name", "engine_name", "description", "inputSchema"};
each call is appended to `calls.jsonl` as {"tool": engine_name, "arguments": {…}}.
"""
from __future__ import annotations

import json
import sys

PROTOCOL = "2024-11-05"
STOP = ("Synapse runs this call and gives you its result on your next turn. "
        "Stop now: write nothing more in this reply.")


def _respond(out, msg_id, result=None, error=None) -> None:
    body = {"jsonrpc": "2.0", "id": msg_id}
    if error is not None:
        body["error"] = error
    else:
        body["result"] = result
    out.write(json.dumps(body) + "\n")
    out.flush()


def serve(tools: list[dict], calls_path: str, stdin=sys.stdin, stdout=sys.stdout) -> None:
    by_name = {t["name"]: t for t in tools}
    # Read-only, truthfully: calling a bridge tool records the call and changes
    # nothing. The engine runs it later under the agent's own policy, approvals
    # included. Without the hint Codex asks for an approval exec mode cannot give
    # ("MCP tool call requires approval, but approval policy is never").
    listed = [{"name": t["name"], "description": t.get("description") or "",
               "inputSchema": t.get("inputSchema") or {"type": "object", "properties": {}},
               "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False}}
              for t in tools]
    for line in stdin:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if not isinstance(msg, dict) or msg.get("id") is None:
            continue                                   # a notification
        method, params = msg.get("method"), msg.get("params") or {}
        if method == "initialize":
            _respond(stdout, msg["id"], {"protocolVersion": params.get("protocolVersion") or PROTOCOL,
                                         "capabilities": {"tools": {}},
                                         "serverInfo": {"name": "synapse", "version": "1"}})
        elif method == "tools/list":
            _respond(stdout, msg["id"], {"tools": listed})
        elif method == "tools/call":
            tool = by_name.get(params.get("name"))
            if tool is None:
                _respond(stdout, msg["id"], {"content": [{"type": "text", "text": f"Unknown tool {params.get('name')!r}."}],
                                             "isError": True})
                continue
            args = params.get("arguments")
            with open(calls_path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"tool": tool.get("engine_name") or tool["name"],
                                    "arguments": args if isinstance(args, dict) else {}}) + "\n")
            _respond(stdout, msg["id"], {"content": [{"type": "text", "text": STOP}], "isError": False})
        elif method == "ping":
            _respond(stdout, msg["id"], {})
        else:
            _respond(stdout, msg["id"], error={"code": -32601, "message": f"Method not found: {method}"})


if __name__ == "__main__":
    with open(sys.argv[1], encoding="utf-8") as f:
        serve(json.load(f), sys.argv[2])
