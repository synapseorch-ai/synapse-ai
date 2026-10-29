"""A plan's Browser step with its fall-back agent, and inside a Loop (F1233; BRW1, D328; CAP3's stage 1, brw-03 and
brw-04).

"If the page has changed, let my Web agent find the way" is `on_miss: "agent"` with a Browser agent. The palette said
so but never listed which agents are Browser agents, and a Browser agent the same plan makes (F1223's `type:
browser`) was refused at the dry run: `_refuse_browser_steps` read the step's agent from the store, where a planned
agent is not yet. Now the palette lists the workspace's Browser agents by id, says a plan may make one, and the dry
run reads the plan's own agents.

"For each order in the list I paste, run Confirm a delivery": a for-each Loop with the Browser step in its body,
its input the Loop's item. A pasted list is the run's own input, which is text, and a Loop over text runs zero times
(F14 keeps that on purpose), so a step before the Loop makes the list (Naveen, 10-29): a Loop straight over the run's
input is sent back saying so. A Browser step in a for-each body that reads nothing of the item would replay the same
thing every time: sent back too.
"""
from __future__ import annotations

import json

import pytest

from _fakes import seed as S
from core import builder_access, builder_tools
from core.browser import recordings
from core.builder_tools import execute_builder_tool_with_events
from core.models_orchestration import OrchestrationRun
from core.native_builder.browser_steps import palette
from core.orchestration.engine import live
from core.orchestration.state import SharedState

ORDER = {"role": "textbox", "name": "Order number", "label": "Order number", "css": "#order"}


def _rec_steps() -> list[dict]:
    return [{"id": "s1", "action": "open", "url": "https://portal.saltmarsh.example/orders"},
            {"id": "s2", "action": "type", "target": ORDER, "value": {"input": "order number"}},
            {"id": "s3", "action": "click", "target": {"role": "button", "name": "Save"}}]


class _Server:
    agent_sessions: dict = {}
    tool_router: dict = {}


@pytest.fixture(autouse=True)
async def world():
    builder_access.forget_runs()
    await S.seed_agents([S.make_agent(id="agent_web", name="Web", type="browser"),
                         S.make_agent(id="agent_desk", name="Front desk")])
    yield (await recordings.create("Confirm a delivery", _rec_steps(), by="you"))["id"]
    builder_access.forget_runs()
    builder_tools.set_browser_check(None)


async def _submit(draft: dict) -> dict:
    run = OrchestrationRun(run_id="builder_brw_loop", orchestration_id="orch_native_builder",
                           shared_state={builder_access.CALLER_KEY: {"user_id": "u1"}, "intent": "create"})
    await SharedState(run).checkpoint()
    with live(run):
        raw, _ = await execute_builder_tool_with_events("submit_plan", draft, _Server(), run_id=run.run_id)
    return json.loads(raw) if isinstance(raw, str) else raw


def _plan(*steps, agents: list | None = None, settings: dict | None = None) -> dict:
    return {"kind": "create", "title": "Confirm deliveries", "summary": "Confirms deliveries on the portal.",
            "steps": list(steps), **({"agents": agents} if agents else {}),
            **({"settings": settings} if settings else {})}


def _browser(rec: str, value: str = "{{state.user_input}}", **extra) -> dict:
    return {"id": "confirm", "type": "browser", "name": "Confirm the delivery", "does": "Confirms it on the portal",
            "config": {"recording_id": rec, "recording_inputs": {"order number": value}}, **extra}


_LISTED = {"id": "orders", "type": "llm", "name": "List the orders", "does": "Lists the pasted order numbers",
           "prompt": "The order numbers in the message, as a JSON list of strings."}
_TYPED = {"state_schema": {"orders": {"type": "list", "default": []}}}


def _loop(over: str = "orders") -> dict:
    return {"id": "each", "type": "loop", "name": "Each order", "does": "Each order in turn", "loop_over": over,
            "body": ["confirm"]}


class TestThePalette:
    async def test_it_lists_the_browser_agents_by_id_and_no_other(self, world):
        text = await palette()
        assert "`agent_web` \"Web\"" in text
        assert "agent_desk" not in text

    async def test_it_says_a_plan_may_make_one(self, world):
        assert "type `browser`" in await palette()

    async def test_without_a_browser_agent_it_says_the_step_pauses(self, world):
        from core.store.resources import delete_agent

        await delete_agent("agent_web")
        text = await palette()
        assert "No Browser agent here" in text and "pause" in text

    async def test_it_says_how_a_loop_runs_one_for_each_item(self, world):
        text = await palette()
        assert "`loop`" in text and "`body`" in text and "{{state.loop_item}}" in text
        assert "state_schema" in text and "`transform`" in text, "a pasted list is made a list first"


class TestTheFallBackAgent:
    async def test_an_existing_browser_agent_is_taken(self, world):
        out = await _submit(_plan(_browser(world, agent="agent_web", config={
            "recording_id": world, "recording_inputs": {"order number": "{{state.user_input}}"}, "on_miss": "agent"})))
        assert out.get("finish") == "submit_plan", out

    async def test_a_browser_agent_the_plan_makes_is_taken(self, world):
        finder = {"name": "Finder", "type": "browser", "purpose": "Finds the way on the portal when a page changed."}
        step = _browser(world, agent="Finder")
        step["config"]["on_miss"] = "agent"
        out = await _submit(_plan(step, agents=[finder]))
        assert out.get("finish") == "submit_plan", out

    async def test_an_agent_that_is_not_a_browser_agent_is_still_sent_back(self, world):
        step = _browser(world, agent="agent_desk")
        step["config"]["on_miss"] = "agent"
        out = await _submit(_plan(step))
        assert "pick a Browser" in json.dumps(out), out


class TestTheLoop:
    async def test_a_browser_step_in_a_loop_over_a_made_list_is_taken(self, world):
        out = await _submit(_plan(_LISTED, _loop(), _browser(world, "{{state.loop_item}}"), settings=_TYPED))
        assert out.get("finish") == "submit_plan", out

    async def test_a_loop_straight_over_the_run_s_input_is_sent_back(self, world):
        out = await _submit(_plan(_loop("user_input"), _browser(world, "{{state.loop_item}}")))
        assert "the run's own input, which is text" in json.dumps(out), out

    async def test_a_browser_step_that_reads_nothing_of_the_item_is_sent_back(self, world):
        out = await _submit(_plan(_LISTED, _loop(), _browser(world, "HP-2211"), settings=_TYPED))
        assert "every item" in json.dumps(out), out


async def test_agents_that_cannot_be_read_are_said_and_logged_never_none_here(world, monkeypatch, caplog):
    from core.store import resources

    async def broken():
        raise RuntimeError("store down")

    monkeypatch.setattr(resources, "load_agents", broken)
    with caplog.at_level("WARNING"):
        text = await palette()
    assert "could not be read" in text and "No Browser agent here" not in text
    assert "agents not listed" in caplog.text
