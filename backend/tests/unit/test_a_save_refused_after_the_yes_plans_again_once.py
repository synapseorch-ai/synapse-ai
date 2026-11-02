"""A save refused after the yes goes back to the planner once, never a dead end (F1295 (b), prod-13; 79 11-02).

prod-13's rerun (a0da2070): after the yes and the Connect card, the save refused ("Nothing was built: it would not pass
validation: …"), and the person's next "yes" heard "Nothing is waiting for a yes". A tool connected after the yes (an
app's or an MCP server's, F949) has no shape to check before it, so a save can still refuse what the card showed. Now
the first such refusal, with nothing of the workflow saved yet, goes back to the planner with the save's own words,
the tools the build made kept, and its next card asks for one more yes, saying why; a second says so plainly, and
stops.
"""
from __future__ import annotations

import json
from pathlib import Path

from core.native_builder import build_save
from test_a_build_makes_the_tools_it_found import _approved, _call, _tool, workspace  # noqa: F401

REFUSED = ("Step 'Send it' sets 'subject', which `mailchimp_send_campaign` does not take: it takes campaign_id, "
           "body.")


async def _made_then_refused(run, monkeypatch):
    made, _ = await _call("save_build", {"only": "tools"}, run)
    assert made["outcome"] == "tools_made", made

    async def refuses(*a, **kw):  # what the save reads once the tool is here, unlike what the card could
        return [REFUSED]

    monkeypatch.setattr(build_save, "dry_issues", refuses)
    return await _call("save_build", {}, run)


async def test_the_first_refusal_goes_back_to_the_planner_with_its_words(client, monkeypatch):
    from core.store.resources import load_orchestrations

    run, _ = await _approved()
    (out, _) = await _made_then_refused(run, monkeypatch)
    assert out["outcome"] == "replan" and "does not take: it takes campaign_id, body" in out["refused"], out
    assert await _tool() is not None, "the tools it made stay, made and connected"
    assert not [o for o in await load_orchestrations() if o.get("name") == "Newsletter"]
    assert run.shared_state["plan_answer"]["outcome"] == "replan"


async def test_the_next_card_says_why_it_asks_for_one_more_yes(client, monkeypatch):
    from core.native_builder import build_plan

    run, _ = await _approved()
    real = build_save.dry_issues
    await _made_then_refused(run, monkeypatch)
    monkeypatch.setattr(build_save, "dry_issues", real)
    draft = run.shared_state[build_plan.DRAFT_KEY]
    checked, _ = await _call("check_build", {"draft": draft}, run)
    assert checked["review"].startswith("Before I saved it, the build needed one change: Step 'Send it' sets "
                                        "'subject'"), checked["review"][:300]
    assert "for one more yes" in checked["review"]


async def test_a_second_refusal_stops_and_says_so_plainly(client, monkeypatch):
    run, _ = await _approved()
    await _made_then_refused(run, monkeypatch)
    again, _ = await _call("save_build", {}, run)
    assert again["outcome"] == "stopped", again
    assert again["reason"].startswith("Nothing was built: it would not pass validation: ")
    assert "I planned it again once already. The tools I made stay" in again["reason"]
    assert await _tool() is not None


def test_the_save_goes_back_to_the_planner_and_otherwise_says_what_it_built():
    path = Path(build_save.__file__).with_name("orchestration.json")
    steps = {s["id"]: s for s in json.loads(path.read_text(encoding="utf-8"))["steps"]}
    assert steps["step_save"]["next_step_id"] == "step_save_route"
    route = steps["step_save_route"]
    assert (route["type"], route["switch_expression"]) == ("switch", "state.build_saved.outcome")
    assert route["switch_cases"] == {"replan": "step_plan_context"}
    assert route["switch_default_step_id"] == "step_build_done" == route["on_exhausted_step_id"]
    assert steps["step_save"]["on_exhausted_step_id"] == "step_build_done"
