"""
"Try again on a stronger model" (F1018, SAI2-P3, D300; Naveen 10-24, the press 10-27).

A turn that failed, or ended with no answer, and changed nothing ends with one offer: the same message once more on
the next model up this workspace can call and pay for, at its price. The press sends the same message on the same
chat's stream with `retry_of`; the server checks that turn and picks the model; the browser never names one.

The server's checks (the manager's, 10-27): the turn is this chat's; it failed and wrote nothing; it is tried again at
most once; the re-run is on the stronger model, as a new turn of its own, under the same cap. The next message is on
the usual model.
"""
from __future__ import annotations

import json

import pytest

from core import chat_turns, try_stronger
from core.llm_providers import LLMError
from core.native_builder import models_menu as MM


def _entry(id, *, tools="native", vision=False, reasoning=False, out=1.0, source="managed", pay=True):
    return MM.ModelEntry(id=id, name=id.split(".")[1], provider=id.split(".")[1].split("-")[0], source=source,
                         tools=tools, vision=vision, reasoning=reasoning, price_in=out / 4, price_out=out,
                         can_pay=pay)


MENU = [_entry("synapse.nova-lite", out=0.24), _entry("synapse.claude-haiku", reasoning=True, out=5),
        _entry("synapse.claude-sonnet", reasoning=True, vision=True, out=15),
        _entry("synapse.claude-opus", reasoning=True, vision=True, out=75)]


@pytest.fixture(autouse=True)
def menu(monkeypatch):
    now = {"menu": list(MENU)}

    async def fake():
        return list(now["menu"])

    monkeypatch.setattr(MM, "menu", fake)
    return now


# ── the offer ─────────────────────────────────────────────────────────────────────────────────────────────────────

async def test_a_failed_turn_is_offered_the_next_model_up_at_its_price():
    made = await try_stronger.offer("synapse.claude-haiku", has_tools=False, retry_of="t1")
    assert made == {"type": "try_stronger", "model": "synapse.claude-sonnet", "name": "claude-sonnet",
                    "price": "about $15 per 1M output tokens",
                    "label": "Try again on claude-sonnet (about $15 per 1M output tokens)", "retry_of": "t1"}


async def test_nothing_is_offered_from_the_strongest_this_workspace_can_pay_for(menu):
    menu["menu"] = [e for e in MENU if e.id != "synapse.claude-opus"]
    assert await try_stronger.offer("synapse.claude-sonnet", has_tools=False, retry_of="t1") is None


async def test_a_model_without_a_key_or_wallet_is_never_offered(menu):
    # The menu lists only what has a key; one the wallet cannot pay for is listed unpaid.
    menu["menu"] = [e if e.id != "synapse.claude-sonnet" else _entry(e.id, reasoning=True, out=15, pay=False)
                    for e in MENU if e.id != "synapse.claude-opus"]
    assert await try_stronger.offer("synapse.claude-haiku", has_tools=False, retry_of="t1") is None


async def test_an_empty_wallet_hides_every_managed_model(menu):
    menu["menu"] = [_entry(e.id, reasoning=e.reasoning, out=e.price_out, pay=False) for e in MENU]
    assert await try_stronger.offer("synapse.nova-lite", has_tools=False, retry_of="t1") is None


# ── an agent's chat ───────────────────────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def model_calls(monkeypatch):
    """The agent loop's model calls: each fails as a provider's refusal does, except on the models in `answers`."""
    import core.react_engine as re_mod

    seen: dict = {"models": [], "answers": set()}

    async def call(*args, **kwargs):
        model = kwargs.get("current_model")
        seen["models"].append(model)
        if model in seen["answers"]:
            return "Here it is."
        raise LLMError("Claude LLM Error: API error 529: overloaded")

    monkeypatch.setattr(re_mod, "llm_generate_response", call)
    return seen


def _frames(text: str) -> list[dict]:
    return [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ")]


async def _say(client, agent, *, session="s1", retry_of=None):
    body = {"message": "Summarise the week", "agent_id": agent["id"], "session_id": session}
    if retry_of:
        body["retry_of"] = retry_of
    return await client.post("/chat/stream", json=body)


async def test_a_failed_chat_turn_ends_with_the_offer(client, seed_agent, model_calls):
    agent = await seed_agent(model="synapse.claude-haiku")
    frames = _frames((await _say(client, agent)).text)
    offered = [f for f in frames if f["type"] == "try_stronger"]
    assert len(offered) == 1 and offered[0]["model"] == "synapse.claude-sonnet"
    assert offered[0]["retry_of"] == chat_turns.find("s1").id
    types = [f["type"] for f in frames]
    assert types.index("try_stronger") < types.index("error"), "a page stops reading a turn at its error"


async def test_the_press_runs_that_turn_once_on_the_stronger_model_then_the_usual_one(client, seed_agent,
                                                                                    model_calls):
    agent = await seed_agent(model="synapse.claude-haiku")
    turn = next(f for f in _frames((await _say(client, agent)).text) if f["type"] == "try_stronger")
    model_calls["answers"] = {"synapse.claude-sonnet", "synapse.claude-haiku"}
    model_calls["models"].clear()
    again = _frames((await _say(client, agent, retry_of=turn["retry_of"])).text)
    assert any(f["type"] == "response" for f in again)
    assert set(model_calls["models"]) == {"synapse.claude-sonnet"}, "the re-run is on the stronger model only"
    model_calls["models"].clear()
    _frames((await _say(client, agent)).text)
    assert set(model_calls["models"]) == {"synapse.claude-haiku"}, "the next message is on the usual model"


async def test_a_turn_is_tried_again_at_most_once(client, seed_agent, model_calls):
    agent = await seed_agent(model="synapse.claude-haiku")
    turn = next(f for f in _frames((await _say(client, agent)).text) if f["type"] == "try_stronger")
    again = _frames((await _say(client, agent, retry_of=turn["retry_of"])).text)
    assert not [f for f in again if f["type"] == "try_stronger"], "a turn tried again is not offered again"
    second = await _say(client, agent, retry_of=turn["retry_of"])
    assert second.status_code == 422


async def test_a_press_from_another_chat_or_agent_is_refused(client, seed_agent, model_calls):
    from _fakes import seed as S

    agent, other = S.make_agent(model="synapse.claude-haiku"), S.make_agent(model="synapse.claude-haiku")
    await S.seed_agents([agent, other])
    turn = next(f for f in _frames((await _say(client, agent)).text) if f["type"] == "try_stronger")
    assert (await _say(client, agent, session="s2", retry_of=turn["retry_of"])).status_code == 422
    assert (await _say(client, other, retry_of=turn["retry_of"])).status_code == 422
    assert (await _say(client, agent, retry_of="not-a-turn")).status_code == 422
    # Still the agent's own to press.
    assert (await _say(client, agent, retry_of=turn["retry_of"])).status_code == 200


async def test_a_turn_that_answered_offers_nothing(client, seed_agent, model_calls):
    agent = await seed_agent(model="synapse.claude-haiku")
    model_calls["answers"] = {"synapse.claude-haiku"}
    assert not [f for f in _frames((await _say(client, agent)).text) if f["type"] == "try_stronger"]


def test_a_cap_or_the_wallet_is_never_mended_by_a_stronger_model():
    for why in ("run_cap", "spend_limit"):
        assert not try_stronger.chat_failed({"no_answer": {"why": why}}, None)
    for why in ("repeating", "no_finish"):
        assert try_stronger.chat_failed({"no_answer": {"why": why}}, None)
    assert not try_stronger.chat_failed({"response": "Here it is."}, None), "F1015's answer turn is an answer"
    assert try_stronger.chat_failed(None, {"type": "error", "kind": "model"})
    assert not try_stronger.chat_failed(None, {"type": "error"}), "the app's own failure is not the model's"


async def test_a_turn_that_changed_something_offers_nothing(monkeypatch):
    from core.tools import tool_hints  # noqa: F401 — the policy's own reading of a tool

    class _Tool:
        def __init__(self, read_only):
            self.annotations = {"readOnlyHint": read_only}
            self.trusted = True

    async def resolve(_server, name, _agent, _custom):
        return {"read_web": _Tool(True), "send_email": _Tool(False)}.get(name)

    async def no_custom():
        return []

    monkeypatch.setattr("core.tools.resolve_one_tool", resolve)
    monkeypatch.setattr("core.routes.tools.load_custom_tools", no_custom)
    assert not await try_stronger.wrote(["read_web"], object())
    assert await try_stronger.wrote(["read_web", "send_email"], object())
    assert await try_stronger.wrote(["vanished_tool"], object()), "a tool that can't be looked up counts as a write"


@pytest.mark.parametrize("agent", [{"type": "orchestrator"}, {"type": "builder"},
                                   {"type": "conversational", "team": {"members": ["a"]}}])
def test_only_a_plain_agents_chat_is_offered(agent, monkeypatch):
    from core import team

    monkeypatch.setattr(team, "config_of", lambda a, **k: object() if a.get("team") else None)
    assert not try_stronger.offers_for(agent)
    assert try_stronger.offers_for({"type": "conversational"})


# ── the Synapse AI panel ──────────────────────────────────────────────────────────────────────────────────────────

SESSION = "builder::panel:c1"


async def _failed_run(run_id="builder_run_failed", *, status="failed", step="step_plan", state=None, session=SESSION):
    from core.models_orchestration import OrchestrationRun
    from core.native_builder import seed
    from core.orchestration.engine import OrchestrationEngine
    from core.orchestration.state import SharedState

    await seed.seed_native_builder()  # the run goes on with the builder's own definition
    run = OrchestrationRun(run_id=run_id, orchestration_id="orch_native_builder", session_id=session, status=status,
                           current_step_id=step,
                           shared_state={OrchestrationEngine.MODEL_OVERRIDE_KEY: "synapse.claude-haiku",
                                         **(state or {})})
    await SharedState(run).checkpoint()
    return run


async def test_a_failed_panel_turn_is_tried_again_on_the_stronger_model():
    await _failed_run()
    made = await try_stronger.panel_retry("builder_run_failed", SESSION)
    assert made == {"model": "synapse.claude-sonnet", "retried_from": "builder_run_failed",
                    "retried_on": "synapse.claude-sonnet"}
    assert (await try_stronger.panel_offer("builder_run_failed"))["model"] == "synapse.claude-sonnet"


@pytest.mark.parametrize("why, kwargs", [
    ("another chat's", {"session": "builder::panel:c2"}),
    ("one that answered", {"status": "completed"}),
    ("one that changed something", {"state": {"_run_writes": [{"kind": "agent", "action": "created"}]}}),
    ("a turn already tried again", {"state": {"retried_from": "builder_run_older"}}),
    ("a code step's failure", {"step": "step_check_plan"}),
])
async def test_a_panel_press_is_refused_for(why, kwargs):
    await _failed_run(**kwargs)
    assert isinstance(await try_stronger.panel_retry("builder_run_failed", SESSION), str), why
    if "session" not in kwargs:  # a run's offer is drawn in its own chat; another chat's press is what is refused
        assert await try_stronger.panel_offer("builder_run_failed") is None, why


async def test_a_panel_turn_is_tried_again_at_most_once():
    await _failed_run()
    await _failed_run("builder_run_again", status="completed",
                      state={"retried_from": "builder_run_failed", "retried_on": "synapse.claude-sonnet"})
    assert await try_stronger.panel_retry("builder_run_failed", SESSION) == try_stronger.TRIED


async def test_a_panel_turn_a_cap_stopped_is_not_tried_again(monkeypatch):
    import core.orchestration.run_cap as run_cap

    async def capped(*_a, **_k):
        return "This run has spent its $0.50 cap."

    monkeypatch.setattr(run_cap, "stop_reason", capped)
    await _failed_run()
    assert isinstance(await try_stronger.panel_retry("builder_run_failed", SESSION), str)


async def test_the_retried_panel_run_records_where_it_came_from_and_runs_on_the_stronger_model(monkeypatch):
    """A new run of its own (so nothing the failed one waited on is carried), recorded, on the server's model, under
    the builder's own cap like any run of it."""
    from core.orchestration.engine import OrchestrationEngine
    from core.routes import builder

    await _failed_run()
    started: dict = {}

    async def fake_run(self, *, initial_input, run_id, session_id, initial_state):
        started.update(run_id=run_id, state=initial_state)
        if False:
            yield {}

    monkeypatch.setattr(OrchestrationEngine, "run", fake_run)
    retried = await try_stronger.panel_retry("builder_run_failed", SESSION)
    request = builder.BuilderChatRequest(message="Build me a digest", client_session_id="c1", model=retried["model"])
    run_id, agen = await builder.prepare_builder_run(request, object(), panel=True, retried=retried)
    async for _ in agen:
        pass
    assert run_id != "builder_run_failed"
    assert started["state"][OrchestrationEngine.MODEL_OVERRIDE_KEY] == "synapse.claude-sonnet"
    assert started["state"]["retried_from"] == "builder_run_failed"
    assert started["state"]["retried_on"] == "synapse.claude-sonnet"


async def test_the_panels_stream_ends_with_the_offer_and_a_retried_runs_does_not():
    from core.routes import builder

    await _failed_run()

    async def events():
        yield {"type": "orchestration_error", "error": "Agent step 'Plan' stopped without an answer"}

    seen = [e async for e in builder.try_stronger_after(events(), "builder_run_failed", retried=False)]
    assert seen[-1]["type"] == "try_stronger" and seen[-1]["retry_of"] == "builder_run_failed"
    seen = [e async for e in builder.try_stronger_after(events(), "builder_run_failed", retried=True)]
    assert [e["type"] for e in seen] == ["orchestration_error"]


def test_both_pages_draw_the_offer_and_send_the_press_as_retry_of():
    from pathlib import Path

    src = Path(__file__).resolve().parents[3] / "frontend" / "src"
    events = (src / "lib" / "builderEvents.ts").read_text()
    provider = (src / "components" / "app" / "BuilderProvider.tsx").read_text()
    panel = (src / "components" / "app" / "BuilderPanel.tsx").read_text()
    chat = (src / "app" / "(app)" / "page.tsx").read_text()
    # The panel: the event is a message under the failure, the press re-sends the asked message with `retry_of`.
    assert "case 'try_stronger':" in events and "kind: 'try_stronger'" in events
    assert "...(retryOf ? { retry_of: retryOf } : {})," in provider
    assert "onClick={() => void send(asked, [], { retryOf })}" in panel
    # An agent's chat: the offer is read before the error ends the turn, and the press sends `retry_of`.
    assert "case 'try_stronger':" in chat and "...(retryOf ? { retry_of: retryOf } : {})" in chat
    assert "await processMessage(payload.content, payload.images, offer.retry_of);" in chat


# ── a helper's writes count (SAI2's review) ───────────────────────────────────────────────────────────────────────

@pytest.fixture
def helper_turn(monkeypatch):
    """A turn whose loop is scripted: what its helper did, then no answer."""
    import core.routes.chat as chat_route

    script: dict = {"events": []}

    async def loop(request, _server, **_kw):
        for event in script["events"]:
            yield event
        yield {"type": "final", "response": "I couldn't finish that.", "no_answer": {"why": "repeating"}}

    class _Tool:
        def __init__(self, read_only):
            self.annotations = {"readOnlyHint": read_only}
            self.trusted = True

    async def resolve(_server, name, _agent, _custom):
        return {"read_web": _Tool(True), "send_email": _Tool(False)}.get(name)

    async def no_custom():
        return []

    monkeypatch.setattr(chat_route, "run_react_loop", loop)
    monkeypatch.setattr("core.tools.resolve_one_tool", resolve)
    monkeypatch.setattr("core.routes.tools.load_custom_tools", no_custom)
    return script


def _helper(tool, *, finished=True):
    events = [{"type": "helper_started", "helper_id": "h1", "helper_name": "Mailer", "task": "send it"},
              {"type": "helper_event", "helper_id": "h1", "helper_name": "Mailer",
               "event": {"type": "tool_result", "tool_name": tool, "preview": "ok"}}]
    return events + ([{"type": "helper_finished", "helper_id": "h1", "helper_name": "Mailer"}] if finished else [])


@pytest.mark.parametrize("events, offered", [
    (_helper("send_email"), False),
    (_helper("read_web"), True),
    (_helper("read_web", finished=False), False),
    ([{"type": "member_event", "member_id": "m1", "event": _helper("send_email")[1]}], False),
], ids=["a helper that wrote", "a helper that only read", "a helper still working", "a member that wrote"])
async def test_a_helpers_or_members_write_counts_as_the_turns(client, seed_agent, helper_turn, events, offered):
    agent = await seed_agent(model="synapse.claude-haiku")
    helper_turn["events"] = events
    frames = _frames((await _say(client, agent)).text)
    assert bool([f for f in frames if f["type"] == "try_stronger"]) is offered


# ── a press refused as "a turn is running" keeps the offer (SAI2's review) ───────────────────────────────────────

async def test_a_press_refused_while_a_turn_runs_can_be_pressed_again(client, seed_agent, model_calls, monkeypatch):
    import core.routes.chat as chat_route

    agent = await seed_agent(model="synapse.claude-haiku")
    turn = next(f for f in _frames((await _say(client, agent)).text) if f["type"] == "try_stronger")
    real = chat_route._start
    calls = {"n": 0}

    def busy_once(request, *a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise chat_turns.TurnAlreadyRunning(chat_turns.find("s1"))
        return real(request, *a, **k)

    monkeypatch.setattr(chat_route, "_start", busy_once)
    assert (await _say(client, agent, retry_of=turn["retry_of"])).status_code == 409
    model_calls["answers"] = {"synapse.claude-sonnet"}
    again = await _say(client, agent, retry_of=turn["retry_of"])
    assert again.status_code == 200 and any(f["type"] == "response" for f in _frames(again.text))
