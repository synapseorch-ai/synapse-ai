"""A plan says every part of what the person asked: each done by the build, run once after it, or not done and why
(F1515, eb's QA B13; Naveen's pick 11-10).

On prod Synapse AI built what was asked and never ran it, though the ask ended "run it once after building": a plan
had nowhere to put that part, and nothing said it was left out. A change already lists every part of the ask
(`submit_change`'s `asked`, F1364); a plan now does too:

* `plan`: this build does it.
* `run_once`: they asked for it to run once when it is built. After the save a Run card is up for the workflow it
  made: they press it, since a run spends credits. The card says so before the yes, under "After your yes".
* `not_done`, with `why_not`: said on the card under "Not in this plan" and again after the save. A part left
  `not_done` with no reason goes back to the planner.

A plan from before (no `asked`) builds as it did, with no Run card.
"""
from __future__ import annotations

import json
from pathlib import Path

from test_a_build_is_saved_in_code import (  # noqa: F401  (workspace: its autouse fixture)
    CHECK,
    TELL,
    WRITER,
    _call,
    _plan,
    _reviewed,
    _workspace,
    _yes,
    workspace,
)

PARTS = [{"part": "a wind warning for Rotterdam", "how": "plan"},
         {"part": "run it once after building", "how": "run_once", "message": "Check today's wind"},
         {"part": "post it on Twitter too", "how": "not_done",
          "why_not": "nothing here posts to Twitter yet: connect it on Apps first"}]


def _run_cards(events: list[dict]) -> list[dict]:
    return [e for e in events if e.get("type") == "action_needed" and e.get("title", "").startswith("Run ")]


async def test_a_part_not_done_is_said_on_the_card_and_after_the_save(client):
    run, checked = await _reviewed(_plan(CHECK, TELL, agents=[WRITER], asked=PARTS))
    assert checked["outcome"] == "review", checked
    review = checked["review"]
    assert "## Not in this plan" in review, review
    assert "- post it on Twitter too: nothing here posts to Twitter yet: connect it on Apps first" in review, review
    await _yes(run)
    out, _ = await _call("save_build", {}, run)
    assert out["outcome"] == "saved", out
    assert "Not in this plan: post it on Twitter too" in out["effect"], out["effect"]


async def test_run_it_once_is_a_run_card_after_the_save(client):
    run, checked = await _reviewed(_plan(CHECK, TELL, agents=[WRITER], asked=PARTS))
    assert "Run it once:" in checked["review"].split("## After your yes", 1)[-1], checked["review"]
    await _yes(run)
    out, events = await _call("save_build", {}, run)
    assert out["outcome"] == "saved", out
    [card] = _run_cards(events)
    assert card["title"] == 'Run "Rotterdam wind warning"', card
    [button] = card["buttons"]
    _, flows, _ = await _workspace()
    assert button["label"] == "Run it" and button["tool"] == "run_orchestration", button
    assert button["arguments"]["message"] == "Check today's wind", button
    assert button["arguments"]["orchestration_id"] == flows["Rotterdam wind warning"]["id"], button
    assert "run it once" in out["effect"].lower(), out["effect"]


async def test_a_part_left_not_done_with_no_reason_goes_back_to_the_planner(client):
    parts = [{"part": "post it on Twitter too", "how": "not_done"}]
    run, _ = await _reviewed(_plan(CHECK, TELL, agents=[WRITER]))
    sent, _ = await _call("submit_plan", _plan(CHECK, TELL, agents=[WRITER], asked=parts), run)
    assert "post it on Twitter too" in sent.get("error", ""), sent
    assert "why_not" in sent["error"], sent


async def test_a_plan_from_before_builds_as_it_did_with_no_run_card(client):
    run, checked = await _reviewed(_plan(CHECK, TELL, agents=[WRITER]))
    assert "## Not in this plan" not in checked["review"]
    await _yes(run)
    out, events = await _call("save_build", {}, run)
    assert out["outcome"] == "saved" and not _run_cards(events), events


def test_the_planner_is_told_to_list_every_part():
    planner = json.loads((Path(__file__).resolve().parents[2] / "core" / "native_builder" / "agents"
                          / "planner.json").read_text("utf-8"))
    said = " ".join(planner["system_prompt"].split())
    assert "`asked`" in said and "`run_once`" in said and "`not_done`" in said and "`why_not`" in said, said[:400]
