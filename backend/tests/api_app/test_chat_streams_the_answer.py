"""
Chat shows the answer while the model writes it (U72).

A 2,500-word answer was a minute of "Analyzing your request…" (C2's U72): the
page got the answer whole when the loop ended. `/chat/stream` now carries the
engine's `answer_delta` and `answer_reset`, and v1's `chat/stream` too
(additive: `response` is still the answer of record).
"""
import json

from _fakes.fake_llm import Streamed, tool_call


def _events(text: str) -> list[dict]:
    out = []
    for block in text.split("\n\n"):
        for line in block.split("\n"):
            if line.startswith("data: "):
                try:
                    out.append(json.loads(line[6:]))
                except ValueError:
                    pass
    return out


async def test_the_app_chat_streams_the_answer(client, seed_agent, fake_llm):
    agent = await seed_agent(id="agent_u72a", name="Writer")
    fake_llm.script([Streamed(["A long ", "answer, ", "word by word."], delay=0.02)])
    resp = await client.post("/chat/stream", json={"message": "write", "session_id": "u72_app",
                                                   "agent_id": agent["id"]})
    events = _events(resp.text)
    deltas = [e for e in events if e["type"] == "answer_delta"]
    assert len(deltas) >= 2
    assert "".join(e["text"] for e in deltas) == "A long answer, word by word."
    response = next(e for e in events if e["type"] == "response")
    assert response["content"] == "A long answer, word by word."
    assert events.index(deltas[-1]) < events.index(response)


async def test_narration_before_a_tool_is_taken_back_in_the_app(client, seed_agent, fake_llm):
    agent = await seed_agent(id="agent_u72b", name="Looker", tools=["all"])
    fake_llm.script([Streamed(["Let me check. ", tool_call("nope_tool", q="x")]), Streamed(["Sunny."])])
    events = _events((await client.post("/chat/stream", json={
        "message": "weather?", "session_id": "u72_app2", "agent_id": agent["id"]})).text)
    kinds = [e["type"] for e in events if e["type"].startswith("answer_")]
    assert kinds[:2] == ["answer_delta", "answer_reset"]
    assert next(e for e in events if e["type"] == "answer_reset")["reason"] == "tool_call"


async def test_v1_stream_carries_the_deltas(client, api_key, seed_agent, fake_llm):
    agent = await seed_agent(id="agent_u72c", name="Api")
    fake_llm.script([Streamed(["Hello ", "there."], delay=0.02)])
    events = _events((await client.post("/api/v1/chat/stream", json={"message": "hi", "agent": agent["id"]},
                                        headers=api_key["headers"])).text)
    assert "".join(e["text"] for e in events if e["type"] == "answer_delta") == "Hello there."
    assert next(e for e in events if e["type"] == "response")["content"] == "Hello there."
