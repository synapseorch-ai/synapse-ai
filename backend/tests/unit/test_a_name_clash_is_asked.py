"""A build named like something already here is the person's question, asked by code, never a dead end (F1395; BLD1's
media dry run, 11-05; Naveen's rulings in BLD1's tab).

On a workspace that had "Morning patio table check for Harborpine Coffee", a new conversation asking for much the same
went submit_plan → F1229's send-back ("a workflow called … is already in this workspace … Never save a second of the
same name") → the planner answered in words → check_change → "I couldn't turn that into something I can make or
change here. Say it another way". Now a new workflow named like one here, or a new agent named like one here, is never
sent back to the planner: `check_build` asks the person once, on the form it asks what only they can say (F1253):

- a workflow: change the one here to do this (the next plan is its update, read as if it were open), or build a new
  one under a name code suggests, free in the workspace; or Stop, the card's own;
- an agent: use the one here (its steps run on it), or make a new one under a suggested free name.

The answer is code's to carry out: the new name is put on the plan, the reused agent on its steps, and a change goes
back through "Read the Workflow to Change" (`refetch`), so the planner plans an update with the workflow before it.
"""
from __future__ import annotations

import json
from pathlib import Path

from _fakes import seed as S
from core.native_builder import build_plan
from core.native_builder.target import LAST_BUILT_KEY

from tests.unit.test_the_build_route_decides_in_code import CHECK, TELL, _call, _check, _plan, _run, workspace  # noqa: F401

ORCH = Path(build_plan.__file__).with_name("orchestration.json")
HERE = "Rotterdam wind warning"


async def _seed_here(name: str = HERE, description: str = "Warns the desk when the wind is strong."):
    await S.seed_orchestrations([S.make_orchestration(id="orch_wind", name=name, description=description,
                                                      entry_step_id="end",
                                                      steps=[{"id": "end", "name": "End", "type": "end"}])])


def _field(out: dict, about: str) -> dict:
    found = [f for f in out.get("form") or [] if about in f.get("label", "")]
    assert found, f"no question about {about}: {out.get('outcome')} {out.get('question') or out.get('say')}"
    return found[0]


async def _answered(run, out: dict, field: dict, option: str):
    run.shared_state["human_response"] = {"form": {field["name"]: option}}
    run.shared_state[build_plan.CHECKED_KEY] = out


# ── the planner is never sent back for a name ─────────────────────────────────────────────────────────────────────
class TestNeverSentBack:
    async def test_submit_plan_finishes_on_a_name_already_here(self, client):
        await _seed_here()
        out, _ = await _call("submit_plan", _plan(CHECK, TELL), await _run())
        assert out.get("finish") == "submit_plan", out

    async def test_the_conversations_own_build_is_asked_about_too(self, client):
        await _seed_here()
        run = await _run(**{LAST_BUILT_KEY: "orch_wind"})
        out, _ = await _call("submit_plan", _plan(CHECK, TELL), run)
        assert out.get("finish") == "submit_plan", out
        checked, _ = await _check(run, out)
        assert checked["outcome"] == "ask", checked


# ── a workflow named like one here ────────────────────────────────────────────────────────────────────────────────
class TestAWorkflow:
    async def test_it_is_asked_with_change_it_or_a_new_name(self, client):
        await _seed_here()
        out, _ = await _check(await _run(), {"finish": "submit_plan", **_plan(CHECK, TELL)})
        assert out["outcome"] == "ask", out
        field = _field(out, f"“{HERE}”")
        assert field["type"] == "choice" and "already here" in field["label"], field
        change, new = field["options"]
        assert change == f"Change “{HERE}” to do this", field
        assert new.startswith("Build a new one named “") and new != f"Build a new one named “{HERE}”", field

    async def test_the_new_name_is_free_and_the_plan_takes_it(self, client):
        await _seed_here()
        await S.seed_orchestrations([S.make_orchestration(id="orch_wind2", name=f"{HERE} 2", entry_step_id="end",
                                                          steps=[{"id": "end", "name": "End", "type": "end"}])])
        run = await _run()
        out, _ = await _check(run, {"finish": "submit_plan", **_plan(CHECK, TELL)})
        field = _field(out, f"“{HERE}”")
        new = field["options"][1]
        name = new.removeprefix("Build a new one named “").removesuffix("”")
        assert name.casefold() not in {HERE.casefold(), f"{HERE} 2".casefold()}, name
        await _answered(run, out, field, new)
        again, _ = await _check(run, {"finish": "submit_plan", **_plan(CHECK, TELL)})
        assert again["outcome"] == "review", again
        assert again["plan"]["title"] == name, again["plan"]["title"]

    async def test_the_suggested_name_carries_what_tells_it_apart(self, client):
        await _seed_here()
        out, _ = await _check(await _run(), {"finish": "submit_plan", **_plan(
            CHECK, TELL, summary="Warn the Delft desk when the wind is strong.")})
        assert _field(out, f"“{HERE}”")["options"][1] == f"Build a new one named “{HERE} – Delft”", out["form"]

    async def test_without_words_of_its_own_it_is_numbered(self, client):
        await _seed_here()
        out, _ = await _check(await _run(), {"finish": "submit_plan", **_plan(CHECK, TELL)})
        assert _field(out, f"“{HERE}”")["options"][1] == f"Build a new one named “{HERE} 2”", out["form"]

    async def test_change_it_reads_that_workflow_as_the_one_to_change(self, client):
        await _seed_here()
        run = await _run()
        out, _ = await _check(run, {"finish": "submit_plan", **_plan(CHECK, TELL)})
        field = _field(out, f"“{HERE}”")
        await _answered(run, out, field, field["options"][0])
        again, _ = await _check(run, {"finish": "submit_plan", **_plan(CHECK, TELL)})
        assert again["outcome"] == "refetch", again
        assert run.shared_state["current_orchestration_id"] == "orch_wind"
        assert run.shared_state["intent"] == "update"
        assert any("Change “" in str(a.get("answer")) for a in again["asked"]), again["asked"]

    async def test_refetch_goes_back_through_reading_the_workflow(self):
        steps = {s["id"]: s for s in json.loads(ORCH.read_text())["steps"]}
        assert steps["step_plan_route"]["switch_cases"].get("refetch") == "step_fetch_existing"
        assert steps["step_fetch_existing"]["next_step_id"] == "step_plan_context"

    async def test_it_is_asked_once(self, client):
        await _seed_here()
        run = await _run()
        out, _ = await _check(run, {"finish": "submit_plan", **_plan(CHECK, TELL)})
        field = _field(out, f"“{HERE}”")
        await _answered(run, out, field, field["options"][1])
        again, _ = await _check(run, {"finish": "submit_plan", **_plan(CHECK, TELL)})
        third, _ = await _check(run, {"finish": "submit_plan", **_plan(CHECK, TELL)})
        assert again["outcome"] == third["outcome"] == "review", (again, third)

    async def test_a_change_while_change_it_waits_is_sent_back_to_the_plan(self, client):
        await _seed_here()
        run = await _run()
        out, _ = await _check(run, {"finish": "submit_plan", **_plan(CHECK, TELL)})
        field = _field(out, f"“{HERE}”")
        await _answered(run, out, field, field["options"][0])
        sent, _ = await _call("submit_change", {"ops": [{"op": "update", "target": {"kind": "orchestration",
                                                                                     "name": HERE},
                                                         "fields": {"description": "Gusts too."}}]}, run)
        assert "submit_plan" in sent.get("error", ""), sent

    async def test_when_the_request_has_asked_enough_it_takes_the_new_name_and_says_so(self, client, monkeypatch):
        from core.native_builder import change_tools

        await _seed_here()
        monkeypatch.setattr(change_tools, "QUESTIONS", 0)
        out, _ = await _check(await _run(), {"finish": "submit_plan", **_plan(CHECK, TELL)})
        assert out["outcome"] == "review", out
        assert out["plan"]["title"] == f"{HERE} 2"
        assert f"“{HERE}” is already here" in out["review"], out["review"]


# ── an agent named like one here ──────────────────────────────────────────────────────────────────────────────────
WRITER = {**TELL, "agent": "Front desk"}
NEW_DESK = [{"name": "Front desk", "purpose": "Drafts delay warnings for the desk"}]


class TestAnAgent:
    async def test_it_is_asked_with_use_it_or_a_new_name(self, client):
        out, _ = await _check(await _run(), {"finish": "submit_plan", **_plan(
            CHECK, WRITER, title="Gust warning", agents=NEW_DESK)})
        assert out["outcome"] == "ask", out
        field = _field(out, "“Front desk”")
        assert field["type"] == "choice" and "agent" in field["label"], field
        assert field["options"] == ["Use the “Front desk” already here", "Make a new one named “Front desk 2”"]

    async def test_use_it_runs_its_steps_on_the_one_here(self, client):
        run = await _run()
        draft = {"finish": "submit_plan", **_plan(CHECK, WRITER, title="Gust warning", agents=NEW_DESK)}
        out, _ = await _check(run, draft)
        field = _field(out, "“Front desk”")
        await _answered(run, out, field, field["options"][0])
        again, _ = await _check(run, draft)
        assert again["outcome"] in ("review", "prompts"), again
        plan = again["plan"]
        assert not [a for a in plan.get("agents") or [] if a["name"] == "Front desk"], plan.get("agents")
        assert [s["agent"] for s in plan["steps"] if s["id"] == "tell"] == ["agent_desk"]
        assert "agent_desk" in plan.get("reuse_agents") or []

    async def test_a_new_name_is_the_new_agents_and_its_steps(self, client):
        run = await _run()
        draft = {"finish": "submit_plan", **_plan(CHECK, WRITER, title="Gust warning", agents=NEW_DESK)}
        out, _ = await _check(run, draft)
        field = _field(out, "“Front desk”")
        await _answered(run, out, field, field["options"][1])
        again, _ = await _check(run, draft)
        assert again["outcome"] in ("review", "prompts"), again
        assert [a["name"] for a in again["plan"]["agents"]] == ["Front desk 2"]
        assert [s["agent"] for s in again["plan"]["steps"] if s["id"] == "tell"] == ["Front desk 2"]


# ── what stays as it was ──────────────────────────────────────────────────────────────────────────────────────────
class TestUnchanged:
    async def test_a_name_of_its_own_is_never_asked(self, client):
        await _seed_here()
        out, _ = await _check(await _run(), {"finish": "submit_plan", **_plan(CHECK, TELL, title="Rotterdam gusts")})
        assert out["outcome"] == "review", out

    async def test_an_update_of_the_one_here_is_never_asked(self, client):
        existing = {"id": "orch_wind", "name": HERE, "description": "", "entry_step_id": "check", "steps": [
            {"id": "check", "name": "Check the weather", "type": "tool", "forced_tool": "rotterdam_weather",
             "output_key": "check", "next_step_id": "done"},
            {"id": "done", "name": "End", "type": "end"}]}
        await _seed_here()
        run = await _run(intent="update", existing_orch=existing, current_orchestration_id="orch_wind")
        out, _ = await _check(run, {"finish": "submit_plan", "kind": "update", "title": HERE, "summary": "Also warn.",
                                    "edits": [{"op": "add_step", "after": "check", "step": TELL}]})
        assert out["outcome"] == "review", out
