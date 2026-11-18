"""Self-hosted Synapse AI refuses a turn on a model that cannot hold what it reads, before anything runs (F1592).

The floor is the engine's long-input floor, 128K, as cloud's (Naveen, 11-14, his second answer: "128K, same as cloud").
The planner's first call is measured here as the engine sends it: a real Synapse AI turn with the model scripted, and
the planner's first call (system prompt, message, history and tools, by the engine's own estimate). It must fit the
floor with room for its answer; if the planner's prompt grows past that, this test fails and the floor is looked at
again (eb, 11-14), rather than the floor going stale.

The route refuses before any run, in words that say what to do: a model that reads 128K, one named
(`ollama pull gpt-oss:20b`), and where to change what chose the model: a pick in Synapse AI's model menu, the
Builder model setting, or the everyday model a turn takes when no Builder model is set. A model whose window is not
known runs.
`GET /api/models` says each known window and the floor, for the pickers.
"""
from __future__ import annotations

import json

import pytest

from core.llm_providers import _OLLAMA_ANSWER_ROOM, ChatCall, _ollama_estimate
from core.native_builder import seed_native_builder
from test_builder_routes import route, stream, tool_call

SMALL = "meta.llama3-8b-instruct-v1:0"  # 8,000 tokens by the engine's table
ASK = [route("create"), tool_call("ask_person", question="What should it do?", about="what")]


@pytest.fixture
async def builder():
    await seed_native_builder()


def _planners_first_call(calls: list[dict]) -> dict:
    """The first call that offers the planner's finish (`submit_plan`): step_plan's first turn."""
    return next(c for c in calls if "submit_plan" in json.dumps(c, default=str))


def _tokens(call: dict) -> int:
    """The engine's own estimate of the call (`llm_providers._ollama_estimate`, F1569: its system prompt, message,
    history and tools, as a local model is sent them, at four characters a token), which sizes a local model's call
    and refuses one longer than the model reads. Each once: the call's `prompt_blocks` and `volatile_tail` are its
    message again, cut for caching."""
    return _ollama_estimate(ChatCall(**{**dict.fromkeys(ChatCall._fields), "system": call.get("sys_prompt") or "",
                                        "prompt": call.get("prompt_msg"), "history": call.get("history_messages"),
                                        "tools": call.get("tools")}))


class TestThePlannersPromptFitsTheFloor:
    async def test_its_first_call_and_its_answer_fit(self, client, builder, fake_llm):
        from core.native_builder import models_menu as MM

        fake_llm.script(list(ASK))
        await stream(client, "/api/builder/chat", {"message": "Build me a workflow that triages support email",
                                                   "client_session_id": "f1592-measure"})
        tokens = _tokens(_planners_first_call(fake_llm.calls))
        print(f"the planner's first call: ~{tokens:,} tokens by the engine's own estimate")
        assert tokens > 10_000, f"{tokens}: the planner's real prompt was measured, not a stub's"
        assert tokens + _OLLAMA_ANSWER_ROOM <= MM.SYNAPSE_AI_MIN_WINDOW, (
            f"the planner's first call is ~{tokens:,} tokens; with {_OLLAMA_ANSWER_ROOM:,} for its answer it no longer "
            f"fits the self-hosted floor of {MM.SYNAPSE_AI_MIN_WINDOW:,}: raise the floor (F1592)")


async def _turn(client, cid: str, **body) -> list[dict]:
    return await stream(client, "/api/builder/chat", {"message": "Build me a workflow that triages support email",
                                                      "client_session_id": cid, **body})


def _refused(events: list[dict]) -> dict:
    assert len(events) == 1, events
    [notice] = events
    assert notice["type"] == "builder_notice" and notice["reason"] == "model_window" and notice["final"] is True
    return notice


class TestATurnOnAModelTooSmallIsRefusedBeforeAnythingRuns:
    async def test_a_pick(self, client, builder, fake_llm):
        notice = _refused(await _turn(client, "f1592-pick", model=SMALL))
        assert SMALL in notice["message"] and "8K" in notice["message"] and "model menu" in notice["message"]
        assert "128K" in notice["message"] and "ollama pull gpt-oss:20b" in notice["message"], "what to do, named"
        assert fake_llm.calls == [], "no model was called"

    async def test_the_builder_model_setting(self, client, builder, fake_llm):
        await client.post("/api/settings", json={"agent_name": "Synapse", "builder_model": SMALL})
        notice = _refused(await _turn(client, "f1592-builder"))
        assert "Builder model" in notice["message"] and "clear it" in notice["message"]
        assert fake_llm.calls == []

    async def test_the_everyday_model_when_no_builder_model_is_set(self, client, builder, fake_llm):
        await client.post("/api/settings", json={"agent_name": "Synapse", "builder_model": "", "model": SMALL})
        notice = _refused(await _turn(client, "f1592-everyday"))
        assert "no Builder model is set" in notice["message"] and "clear it" not in notice["message"]
        assert fake_llm.calls == []


class TestAModelThatFitsOrIsNotKnownRuns:
    @pytest.mark.parametrize("model", ["meta.llama3-1-70b-instruct-v1:0", "oaic.my-gateway-model"],
                             ids=["at-the-floor", "not-known"])
    async def test_it_runs(self, client, builder, fake_llm, model):
        fake_llm.script(list(ASK))
        events = await _turn(client, f"f1592-runs-{model[:4]}", model=model)
        assert not any(e.get("reason") == "model_window" for e in events), events
        assert any(e["type"] == "human_input_required" for e in events), "the planner ran and asked its question"


class TestTheModelsListSaysEachWindow:
    async def test_known_windows_and_the_floor(self, client):
        from core.native_builder import models_menu as MM

        listed = (await client.get("/api/models")).json()
        assert listed["synapse_ai_min_window"] == MM.SYNAPSE_AI_MIN_WINDOW
        assert MM.SYNAPSE_AI_MIN_WINDOW == MM.LONG_WINDOW and "synapse_ai_long_window" not in listed
        assert isinstance(listed["windows"], dict)
        assert all(m in listed["all_available"] and isinstance(t, int) for m, t in listed["windows"].items())
        assert "providers" in listed and "all_available" in listed, "what every reader reads is still there"
