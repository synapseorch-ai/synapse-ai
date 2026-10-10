"""
Every agent behind an OpenAI-compatible endpoint (OAI1).

An OpenAI client sets `base_url` to `{origin}/api/v1/openai` and the `model` to
an agent's id or name. These drive the real engine with the fake model where
the answer's words matter, and the engine's events where the shape of a turn
does (an approval, a failure).
"""
from __future__ import annotations

import json

import pytest

from _fakes import engine_events as E
from _fakes.fake_llm import Streamed, tool_call

BASE = "/api/v1/openai"


def _chunks(text: str) -> list:
    """The SSE body's data lines: dicts, and the literal "[DONE]"."""
    out = []
    for block in text.split("\n\n"):
        for line in block.split("\n"):
            if line.startswith("data: "):
                payload = line[6:]
                out.append(payload if payload == "[DONE]" else json.loads(payload))
    return out


def _content(chunks) -> str:
    return "".join((c["choices"][0]["delta"].get("content") or "")
                   for c in chunks if isinstance(c, dict) and c.get("choices"))


def _body(model, *messages, **extra):
    msgs = list(messages) or [{"role": "user", "content": "hi"}]
    return {"model": model, "messages": msgs, **extra}


# ── Auth and models ──────────────────────────────────────────────────────────

class TestAuth:
    async def test_no_key_is_401_in_openais_shape(self, client):
        resp = await client.get(f"{BASE}/models")
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "missing_api_key"

    async def test_a_wrong_key_is_401(self, client):
        resp = await client.post(f"{BASE}/chat/completions", json=_body("x"),
                                 headers={"Authorization": "Bearer sk-syn-" + "0" * 32})
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "invalid_api_key"


class TestModels:
    async def test_agents_are_listed_as_models(self, client, api_key, seed_agent):
        agent = await seed_agent(id="agent_m1", name="Support")
        resp = await client.get(f"{BASE}/models", headers=api_key["headers"])
        assert resp.status_code == 200
        body = resp.json()
        assert body["object"] == "list"
        entry = next(m for m in body["data"] if m["id"] == agent["id"])
        assert entry["object"] == "model" and entry["name"] == "Support"
        assert entry["synapse"]["type"] == "agent"

    async def test_one_model_by_id(self, client, api_key, seed_agent):
        agent = await seed_agent(id="agent_m2", name="Billing")
        resp = await client.get(f"{BASE}/models/{agent['id']}", headers=api_key["headers"])
        assert resp.status_code == 200 and resp.json()["id"] == agent["id"]


# ── A turn ───────────────────────────────────────────────────────────────────

class TestCompletion:
    async def test_the_agent_answers(self, client, api_key, seed_agent, fake_llm):
        agent = await seed_agent(id="agent_c1", name="Helper")
        fake_llm.script(["Paris is the capital of France."])
        resp = await client.post(f"{BASE}/chat/completions", json=_body(agent["id"]),
                                 headers=api_key["headers"])
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["object"] == "chat.completion" and body["id"].startswith("chatcmpl-")
        assert body["model"] == agent["id"]
        choice = body["choices"][0]
        assert choice["message"] == {"role": "assistant", "content": "Paris is the capital of France."}
        assert choice["finish_reason"] == "stop"
        assert set(body["usage"]) >= {"prompt_tokens", "completion_tokens", "total_tokens"}
        assert body["synapse"]["status"] == "completed"
        assert resp.headers["x-synapse-session"].startswith("oai_")

    async def test_the_model_may_be_the_agents_name_in_any_case(self, client, api_key, seed_agent, fake_llm):
        await seed_agent(id="agent_c2", name="Trip Planner")
        fake_llm.script(["ok"])
        resp = await client.post(f"{BASE}/chat/completions", json=_body("trip planner"),
                                 headers=api_key["headers"])
        assert resp.status_code == 200 and resp.json()["choices"][0]["message"]["content"] == "ok"

    async def test_an_unknown_model_is_404(self, client, api_key, seed_agent):
        await seed_agent()
        resp = await client.post(f"{BASE}/chat/completions", json=_body("gpt-4o"), headers=api_key["headers"])
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "model_not_found"

    async def test_the_messages_are_the_conversation(self, client, api_key, seed_agent, fake_llm):
        agent = await seed_agent(id="agent_c3", name="Memory")
        fake_llm.script(["You said blue."])
        resp = await client.post(f"{BASE}/chat/completions", json=_body(
            agent["id"],
            {"role": "system", "content": "Answer in one sentence."},
            {"role": "user", "content": "My favourite colour is blue."},
            {"role": "assistant", "content": "Noted."},
            {"role": "user", "content": [{"type": "text", "text": "What did I say?"}]},
        ), headers=api_key["headers"])
        assert resp.status_code == 200, resp.text
        call = fake_llm.calls[-1]
        assert call["history_messages"][:2] == [
            {"role": "user", "content": "My favourite colour is blue."},
            {"role": "assistant", "content": "Noted."},
        ]
        assert "What did I say?" in call["prompt_msg"]
        assert "Answer in one sentence." in call["sys_prompt"]

    async def test_nothing_is_saved_as_a_chat(self, client, api_key, seed_agent, fake_llm):
        agent = await seed_agent(id="agent_c4", name="Quiet")
        fake_llm.script(["fine"])
        await client.post(f"{BASE}/chat/completions", json=_body(agent["id"]), headers=api_key["headers"])
        from core.store import sessions as store
        assert not [r for r in await store.load() if r["session_id"].startswith("oai_")]

    async def test_a_model_failure_is_an_openai_error(self, client, api_key, seed_agent, monkeypatch):
        await seed_agent(id="agent_c5", name="Flaky")
        import core.react_engine as re
        from core.llm_providers import LLMError

        async def _boom(*a, **k):
            raise LLMError("upstream 503", status=503)
            yield  # pragma: no cover

        monkeypatch.setattr(re, "run_react_loop", _boom)
        resp = await client.post(f"{BASE}/chat/completions", json=_body("agent_c5"), headers=api_key["headers"])
        assert resp.status_code in (429, 502)
        err = resp.json()["error"]
        assert err["type"] == "server_error" or resp.status_code == 429
        assert "upstream 503" not in err["message"]   # our words, never the provider's (D118)


class TestRefusals:
    @pytest.mark.parametrize("extra,param", [
        ({"tools": [{"type": "function", "function": {"name": "x"}}]}, "tools"),
        ({"functions": [{"name": "x"}]}, "functions"),
        ({"tool_choice": "required"}, "tool_choice"),
        ({"n": 2}, "n"),
    ])
    async def test_what_the_agent_decides_is_not_the_callers(self, client, api_key, seed_agent, extra, param):
        agent = await seed_agent()
        resp = await client.post(f"{BASE}/chat/completions", json=_body(agent["id"], **extra),
                                 headers=api_key["headers"])
        assert resp.status_code == 400
        assert resp.json()["error"]["param"] == param

    async def test_an_empty_tools_list_is_fine(self, client, api_key, seed_agent, fake_llm):
        agent = await seed_agent()
        fake_llm.script(["ok"])
        resp = await client.post(f"{BASE}/chat/completions", json=_body(agent["id"], tools=[], tool_choice="auto"),
                                 headers=api_key["headers"])
        assert resp.status_code == 200

    @pytest.mark.parametrize("messages", [
        [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}],
        [{"role": "user", "content": "hi"}, {"role": "tool", "content": "42", "tool_call_id": "a"},
         {"role": "user", "content": "and?"}],
        [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "https://x.test/a.png"}}]}],
        [{"role": "user", "content": [{"type": "input_audio", "input_audio": {}}]}],
        [],
    ])
    async def test_a_conversation_that_is_not_a_turn_is_400(self, client, api_key, seed_agent, messages):
        agent = await seed_agent()
        resp = await client.post(f"{BASE}/chat/completions",
                                 json={"model": agent["id"], "messages": messages}, headers=api_key["headers"])
        assert resp.status_code == 400, resp.text
        assert resp.json()["error"]["type"] == "invalid_request_error"

    async def test_a_body_that_is_not_json_is_400(self, client, api_key):
        resp = await client.post(f"{BASE}/chat/completions", content=b"{nope",
                                 headers={**api_key["headers"], "Content-Type": "application/json"})
        assert resp.status_code == 400


# ── Streaming ────────────────────────────────────────────────────────────────

class TestStreaming:
    async def test_the_answer_streams_as_chunks(self, client, api_key, seed_agent, fake_llm):
        agent = await seed_agent(id="agent_s1", name="Streamer")
        fake_llm.script([Streamed(["Paris ", "is the ", "capital."], delay=0.02)])
        resp = await client.post(f"{BASE}/chat/completions", json=_body(agent["id"], stream=True),
                                 headers=api_key["headers"])
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        chunks = _chunks(resp.text)
        assert chunks[-1] == "[DONE]"
        dicts = [c for c in chunks if isinstance(c, dict)]
        assert all(c["object"] == "chat.completion.chunk" for c in dicts)
        assert dicts[0]["choices"][0]["delta"]["role"] == "assistant"
        assert _content(dicts) == "Paris is the capital."
        content_chunks = [c for c in dicts if c["choices"][0]["delta"].get("content")]
        assert len(content_chunks) >= 2   # it streamed, not one piece at the end
        assert dicts[-1]["choices"][0]["finish_reason"] == "stop"
        assert len({c["id"] for c in dicts}) == 1

    async def test_usage_comes_last_when_asked(self, client, api_key, seed_agent, fake_llm):
        agent = await seed_agent()
        fake_llm.script([Streamed(["ok"])])
        resp = await client.post(f"{BASE}/chat/completions", json=_body(
            agent["id"], stream=True, stream_options={"include_usage": True}), headers=api_key["headers"])
        dicts = [c for c in _chunks(resp.text) if isinstance(c, dict)]
        assert dicts[-1]["choices"] == [] and "total_tokens" in dicts[-1]["usage"]
        assert not [c for c in dicts[:-1] if "usage" in c]

    async def test_no_usage_chunk_unless_asked(self, client, api_key, seed_agent, fake_llm):
        agent = await seed_agent()
        fake_llm.script([Streamed(["ok"])])
        resp = await client.post(f"{BASE}/chat/completions", json=_body(agent["id"], stream=True),
                                 headers=api_key["headers"])
        assert not [c for c in _chunks(resp.text) if isinstance(c, dict) and "usage" in c]

    async def test_words_before_a_tool_call_stay_and_the_answer_follows(
            self, client, api_key, seed_agent, fake_llm, monkeypatch):
        agent = await seed_agent(id="agent_s2", name="Looker", tools=["all"])
        # The call names a tool the agent lacks: refused, and the loop goes on.
        fake_llm.script([Streamed(["Let me check. ", tool_call("nope_tool", q="x")]),
                         Streamed(["It is ", "sunny."])])
        resp = await client.post(f"{BASE}/chat/completions", json=_body(agent["id"], stream=True),
                                 headers=api_key["headers"])
        text = _content([c for c in _chunks(resp.text) if isinstance(c, dict)])
        assert text == "Let me check.\n\nIt is sunny."

    async def test_an_answer_that_did_not_stream_is_sent_whole(self, client, api_key, seed_agent, monkeypatch):
        await seed_agent(id="agent_s3", name="Whole")
        import core.react_engine as re
        monkeypatch.setattr(re, "run_react_loop", E.gen_from([E.final("All at once.")]))
        resp = await client.post(f"{BASE}/chat/completions", json=_body("agent_s3", stream=True),
                                 headers=api_key["headers"])
        assert _content([c for c in _chunks(resp.text) if isinstance(c, dict)]) == "All at once."

    async def test_a_failure_mid_stream_is_an_error_event(self, client, api_key, seed_agent, monkeypatch):
        await seed_agent(id="agent_s4", name="Breaks")
        import core.react_engine as re
        from core.llm_providers import LLMError

        async def _loop(*a, **k):
            yield {"type": "answer_delta", "text": "Hal", "turn": 1}
            raise LLMError("gone", status=500)

        monkeypatch.setattr(re, "run_react_loop", _loop)
        resp = await client.post(f"{BASE}/chat/completions", json=_body("agent_s4", stream=True),
                                 headers=api_key["headers"])
        chunks = _chunks(resp.text)
        assert chunks[-1] == "[DONE]"
        assert "error" in chunks[-2] and chunks[-2]["error"]["type"] == "server_error"


# ── A person's approval ──────────────────────────────────────────────────────

APPROVAL = {
    "type": "human_input_required", "kind": "tool_approval",
    "prompt": "The agent wants to run **send_email**. Allow it?",
    "tool_name": "send_email", "tool_args": {"to": "a@b.c"}, "category": "external",
    "approval_id": "appr-oai", "fields": [],
}


class TestApproval:
    async def test_a_call_that_needs_a_person_waits_and_the_bell_asks(
            self, client, api_key, seed_agent, monkeypatch):
        await seed_agent(id="agent_a1", name="Mailer")
        import core.react_engine as re
        from core.notifications import hub

        monkeypatch.setattr(re, "run_react_loop", E.gen_from([E.status(), APPROVAL]))
        resp = await client.post(f"{BASE}/chat/completions", json=_body("agent_a1"), headers=api_key["headers"])
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "approve" in body["choices"][0]["message"]["content"]
        assert body["synapse"]["status"] == "awaiting_approval"
        assert body["synapse"]["approval_id"] == "appr-oai"
        session = body["synapse"]["session_id"]

        asked = [n for n in hub.list() if n["kind"] == "tool_approval"
                 and n["data"].get("approval_id") == "appr-oai"]
        assert asked and asked[-1]["data"]["answer_url"] == f"/api/openai/approvals/{session}"

        # Approve in the bell: the turn carries on from where it stopped.
        seen = {}

        async def _resumed(request, *args, **kwargs):
            seen.update(kwargs)
            yield {"type": "final", "response": "Sent it."}

        monkeypatch.setattr(re, "run_react_loop", _resumed)
        answered = await client.post(f"/api/openai/approvals/{session}",
                                     json={"approve": True, "approval_id": "appr-oai"})
        assert answered.status_code == 200, answered.text
        from core import chat_turns
        await chat_turns.find(session).wait()
        assert seen["resume_state"]["pending_call"]["approved"] is True
        assert seen["save_turn"] is False
        item = next(n for n in hub.list() if n["kind"] == "tool_approval"
                    and n["data"].get("approval_id") == "appr-oai")
        assert item["resolved"] and item["resolved_as"] == "allowed"
        assert any(n["kind"] == "run_completed" and "Sent it." in n["body"] for n in hub.list())

    async def test_a_second_answer_is_409(self, client, api_key, seed_agent, monkeypatch):
        await seed_agent(id="agent_a2", name="Mailer")
        import core.react_engine as re
        monkeypatch.setattr(re, "run_react_loop", E.gen_from([{**APPROVAL, "approval_id": "appr-2"}]))
        body = (await client.post(f"{BASE}/chat/completions", json=_body("agent_a2"),
                                  headers=api_key["headers"])).json()
        session = body["synapse"]["session_id"]
        monkeypatch.setattr(re, "run_react_loop", E.gen_from([E.final("ok")]))
        first = await client.post(f"/api/openai/approvals/{session}", json={"approve": False})
        assert first.status_code == 200
        from core import chat_turns
        await chat_turns.find(session).wait()
        second = await client.post(f"/api/openai/approvals/{session}", json={"approve": True})
        assert second.status_code == 409


# ── A workflow agent ─────────────────────────────────────────────────────────

class TestWorkflowAgent:
    async def test_a_workflow_agent_is_a_model_and_answers(self, client, api_key, fake_llm):
        from _fakes import seed as S
        orch = S.make_orchestration(id="orch_oai", entry_step_id="answer", steps=[
            {"id": "answer", "name": "Answer", "type": "llm", "prompt_template": "Reply to: {state.user_input}",
             "model": "gpt-4o", "output_key": "reply", "next_step_id": None}])
        await S.seed_orchestrations([orch])
        agent = S.make_orchestrator_agent("orch_oai", id="agent_orch_oai", name="Flow")
        await S.seed_agents([agent])
        listed = (await client.get(f"{BASE}/models", headers=api_key["headers"])).json()["data"]
        assert next(m for m in listed if m["id"] == agent["id"])["synapse"]["type"] == "workflow"

        fake_llm.script(["From the workflow."])
        resp = await client.post(f"{BASE}/chat/completions", json=_body(agent["id"], {
            "role": "user", "content": "earlier"}, {"role": "assistant", "content": "noted"},
            {"role": "user", "content": "now"}), headers=api_key["headers"])
        assert resp.status_code == 200, resp.text
        assert resp.json()["choices"][0]["message"]["content"] == "From the workflow."


class TestUsage:
    async def test_usage_sums_every_model_call_of_the_turn(self, client, api_key, seed_agent, monkeypatch):
        agent = await seed_agent(id="agent_u1", name="Counter", tools=["all"])
        import core.react_engine as re
        from core import turn_usage
        from core.llm_providers import LLMResult

        replies = iter([tool_call("nope_tool", q="x"), "Done."])

        async def model(*a, **k):
            # As generate_response_ex counts each call: 100 in (40 cached), 10 out.
            turn_usage.add(60, 10, 40, 0)
            return LLMResult(next(replies))

        monkeypatch.setattr(re, "llm_generate_response", model)
        body = (await client.post(f"{BASE}/chat/completions", json=_body(agent["id"]),
                                  headers=api_key["headers"])).json()
        assert body["usage"] == {"prompt_tokens": 200, "completion_tokens": 20, "total_tokens": 220,
                                 "prompt_tokens_details": {"cached_tokens": 80}}
