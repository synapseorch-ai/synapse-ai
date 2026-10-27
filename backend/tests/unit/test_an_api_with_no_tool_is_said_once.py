"""An API the build can't make a tool for is said once, plainly, and the same plan is not shown again (F1121, SAI2-P3,
D300; SAI2's triage of P3c's re-measure, 10-28).

bld-01 (Gmail), bld-02 (Google Calendar, Telegram) and bld-07 (Telegram) went round: research found the provider's API
but no spec a tool can be made from (an OAuth sign-in, a token in the URL's path, or calls it could not read), so the
card said "has a public API, but I haven't read how to call it yet: find who can", and each reply drew the same,
unchanged plan again until "we went back and forth". Now:

- the card says once, in one line per provider, that the build can't make that tool here yet, with the two ways that
  work: an MCP server on the MCP servers screen, or "build without it";
- a reply that leaves the plan as it was gets that line alone, at its own question, never the plan again;
- "build without it" leaves those tools out and the rest builds;
- an API whose spec makes a tool (Mailchimp's key in a header) is made as before.
"""
from __future__ import annotations

import pytest

from core import builder_tools  # noqa: F401 — loads the builder's modules first, as at boot
from core.native_builder import build_plan
from test_the_build_route_decides_in_code import SEND, SPEC, _call, _check, _plan, _run, workspace  # noqa: F401

TELEGRAM = {"id": "alert", "type": "tool", "name": "Alert on Telegram", "does": "Sends the Telegram alert",
            "tool": "send_telegram_message"}
WRITE = {"id": "write", "type": "llm", "name": "Write the alert", "does": "Writes the alert text",
         "prompt": "Write a short alert."}
# What research leaves for Telegram's Bot API: its token sits in the URL's path, so no spec passes (`check_api_spec`).
TELEGRAM_LEAD = {"provider": "Telegram", "how": "api", "sources": ["https://core.telegram.org/bots/api"],
                 "detail": "The Bot API sends messages with a bot token. (Its API spec was not used: its base URL's "
                           "path holds a placeholder: only the start of its host may.)"}
LINE = ("I can't build `send_telegram_message` here yet: Telegram has an API, but I can't make a tool for it yet. "
        "Add an MCP server for it on the MCP servers screen and ask again, or say \"build without it\".")


async def _reviewed(*steps, options):
    run = await _run(plan_findings={"goal": "x", "options": options})
    draft = {"finish": "submit_plan", **_plan(*steps)}
    out, _ = await _check(run, draft)
    return run, draft, out


async def test_an_api_with_no_spec_is_said_in_one_plain_line(client):
    _, _, out = await _reviewed(WRITE, TELEGRAM, options=[TELEGRAM_LEAD])
    assert out["outcome"] == "review"
    assert LINE in out["say"] and out["say"].count("send_telegram_message") == 1
    assert "find who can" not in out["say"] and "haven't read" not in out["say"]
    assert "build" not in out["offers"] and "find" not in out["offers"], "the research has read what there is"
    assert "without:send_telegram_message" in out["offers"]
    assert out["no_tool"] == ["send_telegram_message"]


@pytest.mark.parametrize("words", [
    "Gmail's API signs in with OAuth 2.0: a Google Cloud project, an OAuth client ID and the person's consent screen.",
    "Google Calendar API, authorized with OAuth; each user grants access on Google's consent screen.",
])
async def test_an_api_that_signs_in_with_oauth_is_no_tool_either(client, words):
    """A spec that reads well (a Bearer header) is still no tool when the only key is an OAuth sign-in: a token
    pasted on a card expires within the hour."""
    spec = {**SPEC, "base_url": "https://gmail.googleapis.com/gmail/v1", "host_params": [],
            "endpoints": [{"method": "POST", "path": "/users/me/messages/send", "does": "Sends",
                           "tool": "gmail_send_message"}], "docs_url": "https://developers.google.com/gmail/api"}
    option = {"provider": "Gmail", "how": "api", "detail": words, "needs": "A Google account",
              "sources": ["https://developers.google.com/gmail/api"], "api": spec}
    send = {"id": "send", "type": "tool", "name": "Send the reply", "does": "Sends the Gmail reply",
            "tool": "gmail_send_message"}
    _, _, out = await _reviewed(WRITE, send, options=[option])
    [tool] = out["plan"]["tools"]
    assert (tool["status"], tool["via"]) == ("found", "none")
    assert "I can't build `gmail_send_message` here yet: Gmail has an API" in out["say"]
    assert "## The tools it makes" not in out["review"]


async def test_a_reply_that_leaves_the_plan_as_it_was_gets_the_line_not_the_plan(client):
    run, draft, first = await _reviewed(WRITE, TELEGRAM, options=[TELEGRAM_LEAD])
    run.shared_state["review_feedback"] = {"feedback": "approve"}
    again, _ = await _check(run, draft)  # the planner drew the same plan again
    assert again["outcome"] == "same"
    assert again["digest"] == first["digest"]
    assert "# " not in again["review"] and "## Steps" not in again["review"], "not the plan again"
    assert LINE in again["review"] and again["review"].startswith("The plan above is the same")
    # Kept for the answer: what it offers and what it can't build.
    assert again["offers"] == first["offers"] and again["no_tool"] == ["send_telegram_message"]


async def test_a_plan_that_changed_is_reviewed_again(client):
    run, _, _ = await _reviewed(WRITE, TELEGRAM, options=[TELEGRAM_LEAD])
    changed = {"finish": "submit_plan", **_plan({**WRITE, "name": "Write a shorter alert"}, TELEGRAM)}
    again, _ = await _check(run, changed)
    assert again["outcome"] == "review" and "## Steps" in again["review"]


@pytest.mark.parametrize("said", ["build without it", "Build without it.", "ok, build without them"])
async def test_build_without_it_leaves_those_tools_out_and_builds_the_rest(client, said):
    run, draft, _ = await _reviewed(WRITE, TELEGRAM, options=[TELEGRAM_LEAD])
    run.shared_state["review_feedback"] = {"feedback": said}
    answer, _ = await _call("read_plan_answer", {}, run)
    assert answer == {"outcome": "without", "without": ["send_telegram_message"]}
    run.shared_state[build_plan.ANSWER_KEY] = answer
    out, _ = await _check(run, draft)
    assert out["outcome"] in ("review", "prompts"), out.get("say")
    assert "send_telegram_message" not in [t["name"] for t in (out["plan"] or {}).get("tools") or []]
    if out["outcome"] == "review":
        assert "build" in out["offers"]


async def test_a_spec_a_tool_can_be_made_from_is_made_as_before(client):
    spec = {**SPEC, "endpoints": [{**SPEC["endpoints"][0], "tool": "send_mailchimp_campaign"}]}
    option = {"provider": "Mailchimp", "how": "api", "detail": "Marketing API; OAuth 2 for apps, or an API key.",
              "needs": "A Mailchimp account and an API key", "sources": ["https://mailchimp.com/developer/"],
              "api": spec}
    _, _, out = await _reviewed(SEND, options=[option])
    [tool] = out["plan"]["tools"]
    assert (tool["status"], tool["via"]) == ("found", "http_tool"), "a key in a header: the build makes it"
    assert "I can't build" not in out["say"] and out.get("no_tool", []) == []


@pytest.mark.parametrize("said", [
    # bld-07's own words (pro bwa1/sim/results/raw/oss-cli-claude-haiku/bld-07), and bld-01's.
    "I'll need the Telegram bot token and chat ID so the send_telegram_message step can be configured.",
    "Please add the Telegram integration so we have the send message action.",
    "Please go ahead and connect Telegram so we have the send action.",
])
async def test_asking_to_add_the_integration_gets_the_line_without_another_plan(client, said):
    """P5's read of bld-01/02/07 (10-28): the person asks, round after round, for the integration to be added. The
    answer is the plain line at its own question, with no planner call and no plan drawn again."""
    run, _, _ = await _reviewed(WRITE, TELEGRAM, options=[TELEGRAM_LEAD])
    run.shared_state["review_feedback"] = {"feedback": said}
    answer, _ = await _call("read_plan_answer", {}, run)
    assert answer == {"outcome": "no_tool"}
    assert LINE in run.shared_state[build_plan.CHECKED_KEY]["same_say"]


def test_the_line_is_its_own_question_and_never_redraws_the_plan():
    """The definition: a plan the same as the last, or an ask to add what can't be made, goes to its own question,
    whose context is no plan (so the panel draws no card). Its answer is read as a review's answer that is not a yes,
    and never reaches the approval from there: on that path the check rewrites the plan, so a yes must come at a
    review the person saw (F778's binding, `test_nothing_rewrites_what_was_approved_between_the_review_and_the_save`)."""
    from core.native_builder import bundled_definitions

    steps = {s["id"]: s for s in bundled_definitions()["orchestration"]["steps"]}
    assert steps["step_plan_route"]["switch_cases"]["same"] == "step_plan_same"
    assert steps["step_plan_answer_route"]["switch_cases"]["no_tool"] == "step_plan_same"
    same = steps["step_plan_same"]
    assert same["type"] == "human" and same["human_prompt"] == "{state.plan_checked.same_say}"
    assert same["input_keys"] == ["plan_answer"], "never plan_checked: the panel would draw the plan card again"
    assert (same["output_key"], same["next_step_id"]) == ("review_feedback", "step_plan_answer")
    assert same["human_fields"] == steps["step_human_review"]["human_fields"]


async def test_a_yes_to_a_plan_that_cannot_be_built_gets_the_line(client):
    run, _, _ = await _reviewed(WRITE, TELEGRAM, options=[TELEGRAM_LEAD])
    run.shared_state["review_feedback"] = {"feedback": "approve"}
    answer, _ = await _call("read_plan_answer", {}, run)
    assert answer == {"outcome": "no_tool"}


class _WithTelegram:
    """The workspace once the person added Telegram's MCP server, as the router keys its tools."""
    agent_sessions: dict = {}
    tool_router: dict = {"telegram__send_message": object(), "telegram__get_updates": object()}


async def _answer_with(run, server):
    import json

    from core.builder_tools import execute_builder_tool_with_events
    from core.orchestration.engine import live

    with live(run):
        raw, _ = await execute_builder_tool_with_events("read_plan_answer", {}, server, run_id=run.run_id)
    return json.loads(raw) if isinstance(raw, str) else raw


@pytest.mark.parametrize("said", ["I set up the Telegram MCP server, try again.", "please add Telegram",
                                  "approve"])
async def test_once_it_is_here_the_plan_is_checked_again_not_the_line(client, said):
    """SAI2's review of F1121: the line says to add an MCP server and ask again, so asking again after doing it must
    get past the line: what the workspace provides is read again first, and a provider now here is a re-plan on its
    real tools, as the Connect card's "Connected …" is."""
    run, _, _ = await _reviewed(WRITE, TELEGRAM, options=[TELEGRAM_LEAD])
    run.shared_state["review_feedback"] = {"feedback": said}
    assert await _answer_with(run, _WithTelegram()) == {"outcome": "connected"}


@pytest.mark.parametrize("said", ["I set up the Telegram MCP server, try again.", "please add Telegram"])
async def test_with_nothing_new_here_it_is_the_line(client, said):
    run, _, _ = await _reviewed(WRITE, TELEGRAM, options=[TELEGRAM_LEAD])
    run.shared_state["review_feedback"] = {"feedback": said}
    assert (await _call("read_plan_answer", {}, run))[0] == {"outcome": "no_tool"}
