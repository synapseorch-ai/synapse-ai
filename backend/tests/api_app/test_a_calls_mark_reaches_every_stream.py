"""A call's mark reaches every stream that carries its result: the app's chat and the public API's (F1752).

The engine marks a call that ran and failed `failed`, with its reason (F1751). The app's chat stream built its
`tool_result` frame from the keys it named, `refused` among them and `failed` not, so the chat said a call failed only
once it was reopened, never while it ran. Both streams now carry the mark. The public stream carries the reason only
when the frame's own preview already says it: a public read gains no words it did not carry.

Near misses: a call that ran and worked carries neither mark; a refused call keeps its frame as before.
"""
from __future__ import annotations

import json

from _fakes import engine_events as E

FAILED = {"type": "tool_result", "tool_name": "send_note", "failed": True, "reason": "HTTP 405: Example Domain",
          "preview": "Tool 'send_note' FAILED: HTTP 405: Example Domain. It did not do what was asked: tell the person "
                     "it failed, and never say it was sent or done."}
UNSAID = {**FAILED, "tool_name": "post_note", "reason": "words the preview never carried"}


def _frames(text: str) -> list[dict]:
    out = []
    for line in text.split("\n"):
        if line.startswith("data: "):
            try:
                out.append(json.loads(line[6:]))
            except ValueError:
                pass
    return out


def _results(frames, tool):
    return [f for f in frames if f.get("type") == "tool_result" and f.get("tool_name") == tool]


def _turn(*results):
    """A turn that calls each tool in turn and gets each result, then answers."""
    events: list[dict] = []
    for result in results:
        events += [E.tool_execution(result["tool_name"]), result]
    return E.gen_from(events + [E.final("It could not be sent.", intent="chat")])


async def test_the_public_answer_lists_a_call_not_run_with_its_previews_words(client, api_key, seed_agent,
                                                                               monkeypatch):
    import core.react_engine as re_mod

    agent = await seed_agent(id="agent_f1752_sync", name="Api")
    not_run = {"type": "tool_result", "tool_name": "send_fax", "preview": "Error: tool not found", "refused": True,
               "reason": "tool not found"}
    monkeypatch.setattr(re_mod, "run_react_loop", _turn(not_run, E.tool_result("read_stock", "12 left")))
    body = (await client.post("/api/v1/chat", json={"message": "fax it", "agent": agent["id"]},
                              headers=api_key["headers"])).json()
    assert body["refused_calls"] == [{"tool_name": "send_fax", "category": None, "reason": "tool not found"}]


async def test_the_apps_chat_carries_a_failed_calls_mark_live(client, monkeypatch):
    import core.routes.chat as chat

    monkeypatch.setattr(chat, "run_react_loop", _turn(FAILED, E.tool_result("read_stock", "12 left")))
    frames = _frames((await client.post("/chat/stream", json={"message": "send it"})).text)
    [failed] = _results(frames, "send_note")
    assert failed.get("failed") is True and failed["reason"] == "HTTP 405: Example Domain"
    [ran] = _results(frames, "read_stock")
    assert "failed" not in ran and "refused" not in ran


async def test_the_public_stream_carries_the_mark_and_only_a_reason_its_preview_says(client, api_key, seed_agent,
                                                                                     monkeypatch):
    import core.react_engine as re_mod

    agent = await seed_agent(id="agent_f1752_api", name="Api")
    monkeypatch.setattr(re_mod, "run_react_loop", _turn(FAILED, UNSAID, E.tool_result("read_stock", "12 left")))
    frames = _frames((await client.post("/api/v1/chat/stream", json={"message": "send it", "agent": agent["id"]},
                                        headers=api_key["headers"])).text)
    [failed] = _results(frames, "send_note")
    assert failed.get("failed") is True and failed["reason"] == "HTTP 405: Example Domain"
    [unsaid] = _results(frames, "post_note")
    assert unsaid.get("failed") is True and "reason" not in unsaid, "no words its preview did not carry"
    [ran] = _results(frames, "read_stock")
    assert "failed" not in ran and "refused" not in ran
