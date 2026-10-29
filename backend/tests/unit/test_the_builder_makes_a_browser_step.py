"""The builder makes a Browser step and a Browser agent only as the app would save them (F1189; BRW1, D328).

Before F1189 the builder's step tools took `type: "browser"` with no way to name its recording, and none of them ran
F1146's save rule, so a Browser step with no recording saved (it then stopped every run). `update_step` and
`update_agent` passed a step's `browser_sessions` and an agent's `browser` straight through: no one-per-site, no
"only your own or shared with you" (F1167), no "the recording exists". Each door now checks what the app's editor
checks, from one module (`core.browser.save_checks`), plus what a model gets wrong and a person would not: a
recording's inputs filled, and `may_send_steps` naming the recording's own steps.

A host plugs in what it decides itself (BRW3's binding): whether the Browser is on its plan (`set_browser_check`),
which agent types its route allows (`set_agent_types`), and its own pick rule (`set_pick_check`).
"""
from __future__ import annotations

import json
import time
import types

import pytest

from _fakes import seed as S
from core import builder_tools, site_sessions
from core.browser import recordings
from core.builder_tools import execute_builder_tool
from core.storage import get_blob_store, set_blob_store
from core.storage.base import LocalBlobStore

ORDER = {"role": "textbox", "name": "Order number", "label": "Order number", "css": "#order"}


def _rec_steps() -> list[dict]:
    return [{"id": "s1", "action": "open", "url": "https://portal.saltmarsh.example/"},
            {"id": "s2", "action": "type", "target": ORDER, "value": {"input": "order number"}},
            {"id": "s3", "action": "click", "target": {"role": "button", "name": "Submit"}}]


def _state() -> dict:
    return {"cookies": [{"name": "sid", "value": "a-cookie", "domain": "portal.saltmarsh.example", "path": "/",
                         "expires": time.time() + 86_400}], "origins": []}


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("SYNAPSE_HOME", str(tmp_path / "home"))
    monkeypatch.delenv(site_sessions.KEY_ENV, raising=False)
    was = get_blob_store()
    set_blob_store(LocalBlobStore(tmp_path / "blobs"))
    yield
    set_blob_store(was)
    builder_tools.set_browser_check(None)
    builder_tools.set_pick_check(None)
    builder_tools.set_agent_types(None)


@pytest.fixture
async def world():
    """A workflow, a recording, the person's own session for saltmarsh.example, and Priya's for ledger.example,
    which she has not shared."""
    me = site_sessions.install_person()
    rec = await recordings.create("Find an order", _rec_steps(), by=me)
    mine = await site_sessions.keep(None, state=_state(), site="portal.saltmarsh.example", owner=me)
    also_mine = await site_sessions.keep(None, state=_state(), site="www.saltmarsh.example", owner=me)
    priyas = await site_sessions.keep(None, state=_state(), site="ledger.example", owner="priya")
    browser_agent = S.make_agent(id="agent_web", name="Web", type="browser")
    await S.seed_agents([browser_agent, S.make_agent(id="agent_chat", name="Chat")])
    await S.seed_orchestrations([S.make_orchestration(id="orch_b")])
    return types.SimpleNamespace(rec=rec["id"], mine=mine, also_mine=also_mine, priyas=priyas, me=me)


async def call(tool: str, args: dict) -> dict:
    return json.loads(await execute_builder_tool(tool, args, types.SimpleNamespace(agent_sessions={})))


async def _steps(orch_id: str = "orch_b") -> dict:
    from core.routes.orchestrations import load_orchestrations
    orch = next((o for o in await load_orchestrations() if o["id"] == orch_id), None)
    return {s["id"]: s for s in (orch or {}).get("steps") or []}


async def _agent(agent_id: str) -> dict | None:
    from core.store.resources import get_agent
    return await get_agent(agent_id)


def _browser_step(**fields) -> dict:
    return {"step_id": "step_web", "name": "Find it", "type": "browser", "next_step_id": None, **fields}


def _good(w, **more) -> dict:
    return {"recording_id": w.rec, "recording_inputs": {"order number": "{{ state.user_input }}"}, **more}


async def _write(door: str, step: dict) -> dict:
    """`step` (flat, as `add_single_step` takes it) written through `door` onto workflow `orch_b`."""
    whole = {("id" if k == "step_id" else k): v for k, v in step.items()}
    if door == "add_single_step":
        return await call(door, {"orch_id": "orch_b", **step})
    if door == "add_multiple_steps":
        return await call(door, {"orch_id": "orch_b", "steps": [step]})
    if door == "add_steps":
        return await call(door, {"orch_id": "orch_b", "steps_json": json.dumps([whole])})
    if door == "update_step":
        patch = {k: v for k, v in whole.items() if k not in ("id", "next_step_id")}
        return await call(door, {"orch_id": "orch_b", "step_id": "step_1", "patch_json": json.dumps(patch)})
    if door == "update_orchestration":
        return await call(door, {"orch_id": "orch_b", "entry_step_id": whole["id"], "steps_json": json.dumps([whole])})
    if door == "create_orchestration":
        return await call(door, {"name": "Orders", "entry_step_id": whole["id"], "steps_json": json.dumps([whole])})
    raise AssertionError(door)


DOORS = ["add_single_step", "add_multiple_steps", "add_steps", "update_step", "update_orchestration",
         "create_orchestration"]


async def _unchanged(before: dict) -> None:
    from core.routes.orchestrations import load_orchestrations
    assert await _steps() == before
    assert [o for o in await load_orchestrations() if o.get("name") == "Orders"] == []


class TestABrowserStepNeedsItsRecording:
    @pytest.mark.parametrize("door", DOORS)
    async def test_without_one_it_is_refused_at_every_door(self, world, door):
        before = await _steps()
        result = await _write(door, _browser_step())
        assert "needs a recording" in result.get("error", ""), result
        await _unchanged(before)

    @pytest.mark.parametrize("door", DOORS)
    async def test_one_that_does_not_exist_is_refused_at_every_door(self, world, door):
        before = await _steps()
        result = await _write(door, _browser_step(recording_id="rec_00000000000000000000"))
        assert "No such recording: rec_00000000000000000000" in result.get("error", ""), result
        await _unchanged(before)

    async def test_with_its_recording_and_inputs_it_saves_every_field(self, world):
        result = await call("add_single_step", {"orch_id": "orch_b", **_browser_step(
            recording_id=world.rec, recording_inputs_json=json.dumps({"order number": "{{ state.user_input }}"}),
            may_send_steps=["s3"], on_miss="agent", agent_id="agent_web")})
        assert "error" not in result, result
        step = (await _steps())["step_web"]
        assert step["recording_id"] == world.rec
        assert step["recording_inputs"] == {"order number": "{{ state.user_input }}"}
        assert step["may_send_steps"] == ["s3"] and step["on_miss"] == "agent" and step["agent_id"] == "agent_web"

    async def test_an_input_the_recording_has_not_got_is_refused(self, world):
        result = await _write("add_single_step", _browser_step(**_good(world, recording_inputs={
            "order number": "{{ state.user_input }}", "colour": "red"})))
        assert "colour" in result["error"] and "order number" in result["error"], result

    async def test_an_input_left_unfilled_is_refused_naming_it(self, world):
        result = await _write("add_single_step", _browser_step(recording_id=world.rec))
        assert "order number" in result["error"], result

    async def test_may_send_names_only_the_recording_s_own_steps(self, world):
        result = await _write("add_single_step", _browser_step(**_good(world, may_send_steps=["s3", "s9"])))
        assert "s9" in result["error"], result

    async def test_an_agent_to_find_the_way_must_be_a_browser_agent(self, world):
        result = await _write("add_single_step", _browser_step(**_good(world, on_miss="agent",
                                                                      agent_id="agent_chat")))
        assert "pick a Browser agent" in result["error"], result

    async def test_the_schema_offers_the_step_s_fields(self):
        from core.builder_tools import BUILDER_TOOL_SCHEMAS
        props = next(s["function"]["parameters"]["properties"] for s in BUILDER_TOOL_SCHEMAS
                     if s["function"]["name"] == "add_single_step")
        for field in ("recording_id", "recording_inputs_json", "may_send_steps", "on_miss", "browser_sessions"):
            assert field in props, field


class TestAStepsSessions:
    @pytest.mark.parametrize("door", DOORS)
    async def test_someone_else_s_unshared_session_is_refused_at_every_door(self, world, door):
        before = await _steps()
        result = await _write(door, _browser_step(**_good(world, browser_sessions=[world.priyas])))
        assert "isn't shared with you" in result.get("error", ""), result
        assert result.get("refused") == "pick"
        await _unchanged(before)

    async def test_two_for_one_site_are_refused(self, world):
        result = await _write("add_single_step", _browser_step(**_good(world, browser_sessions=[
            world.mine, world.also_mine])))
        assert "Pick one session for saltmarsh.example" in result["error"], result

    async def test_a_session_named_by_its_site_is_the_person_s_own(self, world):
        await site_sessions.forget(world.also_mine)
        result = await _write("add_single_step", _browser_step(**_good(world, browser_sessions=["saltmarsh.example"])))
        assert "error" not in result, result
        assert (await _steps())["step_web"]["browser_sessions"] == [world.mine]

    async def test_a_site_with_two_of_the_person_s_sessions_asks_which(self, world):
        result = await _write("add_single_step", _browser_step(**_good(world, browser_sessions=["saltmarsh.example"])))
        assert "more than one session of yours for saltmarsh.example" in result["error"], result
        assert world.mine in result["error"] and world.also_mine in result["error"]

    async def test_a_site_the_person_has_no_session_for_is_refused(self, world):
        result = await _write("add_single_step", _browser_step(**_good(world, browser_sessions=["ledger.example"])))
        assert "ledger.example" in result["error"] and "Sessions" in result["error"], result

    async def test_a_session_the_step_already_had_stays(self, world):
        flow = S.make_orchestration(id="orch_kept", steps=[{"id": "web", "name": "Web", "type": "browser",
                                                            "recording_id": world.rec, "next_step_id": None,
                                                            "recording_inputs": {"order number": "x"},
                                                            "browser_sessions": [world.priyas]}])
        await S.seed_orchestrations([flow])
        result = await call("update_step", {"orch_id": "orch_kept", "step_id": "web",
                                            "patch_json": json.dumps({"name": "Find the order"})})
        assert "error" not in result, result


class TestABrowserAgent:
    async def test_one_is_made_with_its_sites_sessions_and_recordings(self, world):
        await site_sessions.forget(world.also_mine)
        result = await call("create_agent", {"name": "Orders", "type": "browser", "system_prompt": "x",
                                             "browser": {"sites": ["portal.saltmarsh.example"],
                                                         "sessions": ["saltmarsh.example"],
                                                         "recordings": [world.rec]}})
        assert "error" not in result, result
        saved = await _agent(result["agent"]["id"])
        assert saved["type"] == "browser"
        assert saved["browser"]["sessions"] == [world.mine] and saved["browser"]["recordings"] == [world.rec]
        assert saved["browser"]["sites"] == ["portal.saltmarsh.example"]

    @pytest.mark.parametrize("door", ["create_agent", "create_agents", "update_agent"])
    async def test_someone_else_s_unshared_session_is_refused(self, world, door):
        browser = {"sites": ["ledger.example"], "sessions": [world.priyas]}
        if door == "create_agent":
            result = await call(door, {"name": "Ledger", "type": "browser", "system_prompt": "x", "browser": browser})
        elif door == "create_agents":
            result = await call(door, {"agents": [{"name": "Ledger", "description": "d", "type": "browser",
                                                   "tools": [], "system_prompt": "x", "browser": browser}]})
            result = {"error": json.dumps(result.get("errors") or result)} if "error" not in result else result
        else:
            result = await call(door, {"agent_id": "agent_web", "fields": {"browser": browser}})
        assert "isn't shared with you" in result.get("error", ""), result
        assert world.priyas not in json.dumps(await _agent("agent_web"))

    async def test_a_recording_that_does_not_exist_is_refused(self, world):
        result = await call("update_agent", {"agent_id": "agent_web", "fields": {
            "browser": {"recordings": ["rec_00000000000000000000"]}}})
        assert "No such recording" in result["error"], result

    async def test_browser_settings_on_an_agent_that_is_not_a_browser_agent_are_refused(self, world):
        result = await call("update_agent", {"agent_id": "agent_chat", "fields": {"browser": {"sites": ["x.example"]}}})
        assert "Browser agent" in result["error"], result

    async def test_an_agent_s_settings_in_the_wrong_shape_are_refused(self, world):
        result = await call("update_agent", {"agent_id": "agent_web", "fields": {"browser": {"action_budget": 5000}}})
        assert "action_budget" in result["error"], result

    @pytest.mark.parametrize("kind", ["builder", "tweet"])
    async def test_a_type_the_app_would_refuse_is_refused(self, world, kind):
        result = await call("create_agent", {"name": "Odd", "type": kind, "system_prompt": "x"})
        assert "is not an agent type" in result["error"], result

    def test_the_schema_offers_the_browser_type_and_its_settings(self):
        from core.builder_tools import BUILDER_TOOL_SCHEMAS
        props = next(s["function"]["parameters"]["properties"] for s in BUILDER_TOOL_SCHEMAS
                     if s["function"]["name"] == "create_agent")
        assert "browser" in props["type"]["enum"]
        assert set(props["browser"]["properties"]) >= {"sites", "sessions", "recordings", "mode", "action_budget"}


class TestTheHostDecides:
    async def test_the_browser_off_its_plan_refuses_each_browser_part(self, world):
        asked = []

        async def check(kind):
            asked.append(kind)
            return "Browser agents are not available yet."

        builder_tools.set_browser_check(check)
        step = await _write("add_single_step", _browser_step(**_good(world)))
        agent = await call("create_agent", {"name": "W", "type": "browser", "system_prompt": "x"})
        sites = await call("update_agent", {"agent_id": "agent_web", "fields": {"browser": {"sites": ["a.example"]}}})
        for result in (step, agent, sites):
            assert result == {"error": "Browser agents are not available yet.", "refused": "plan_limit"}
        assert asked == ["step", "agent", "agent_browser"]
        assert "step_web" not in await _steps()

    async def test_the_check_is_not_asked_for_anything_else(self, world):
        async def check(kind):
            raise AssertionError(f"asked for {kind}")

        builder_tools.set_browser_check(check)
        assert "error" not in await call("create_agent", {"name": "Plain", "system_prompt": "x"})
        assert "error" not in await _write("add_single_step", {"step_id": "step_p", "name": "P", "type": "print",
                                                               "print_content": "hi"})

    async def test_a_host_s_pick_rule_decides_and_is_given_what_was_kept(self, world):
        seen = []

        async def picks(caller, lists, stored):
            seen.append((lists, stored))
            return "That session is someone else's."

        builder_tools.set_pick_check(picks)
        result = await _write("add_single_step", _browser_step(**_good(world, browser_sessions=[world.mine])))
        assert result == {"error": "That session is someone else's.", "refused": "pick"}
        assert seen == [([[world.mine]], [[]])]

    async def test_a_host_s_agent_types_decide(self, world):
        builder_tools.set_agent_types(frozenset({"conversational", "delegate"}),
                                      "An agent's type is conversational or delegate (a team).")
        result = await call("create_agent", {"name": "Coder", "type": "code", "system_prompt": "x"})
        assert result["error"] == "An agent's type is conversational or delegate (a team)."
