"""
"Build without X" takes out what only X's steps needed, and the card says it in the person's words (F1426, BLD1;
Naveen's scratch, 11-06, run builder_77a81e4bb602).

Naveen replied `build without twitter_x_post_tweet_x` to a plan that drafts tweets, asks for a yes, then posts each
in a loop. `_dropped` took out "Post Tweet", the only step in the loop "Post Each Tweet", so the loop had no body; the
check failed it, and its own text went to the person word for word, twice: "step post_loop: a loop names its body
steps". Now:

- a loop, or a parallel, left with nothing in it is taken out too, and so is a step that only fed what was taken out
  (a transform, an extract_json or an llm step whose output nothing kept reads); what led to them goes where they
  went, as a dropped tool step's arrivals already do. A tool, human or agent step is never taken out as a feeder;
- the card says what went and why: "Without `twitter_x_post_tweet_x`, “Post Each Tweet” has nothing left to do, so I
  took it out, with “Extract Tweet List” and “Get Tweets Array”, which only fed it.";
- a check's own text never reaches the person: the steps are named by their names, and the raw text stays in the
  result's `issues` for the planner.
"""
from __future__ import annotations

import pytest

from core import builder_tools  # noqa: F401 — loads the builder's modules first, as at boot
from core.native_builder import build_plan
from test_the_build_route_decides_in_code import CHECK, _check, _plan, _run, workspace  # noqa: F401

POST = "twitter_x_post_tweet_x"
NO_WAY = [{"provider": "Twitter (X)", "how": "none", "detail": "no public way in that I could find",
           "sources": ["https://developer.twitter.com/"]}]

DRAFT = {"id": "draft_tweets", "type": "llm", "name": "Draft Tweets", "does": "Writes 5 tweets",
         "prompt": "Write 5 tweets about the shop from {{state.check}}."}
REVIEW = {"id": "review", "type": "human", "name": "Review Tweets", "does": "Asks for a yes before posting",
          "prompt": "Post these?\n\n{{state.draft_tweets}}",
          "config": {"human_fields": [{"name": "approved", "type": "yes_no", "label": "Post these tweets?"}]}}
APPROVED = {"id": "check_approved", "type": "if_else", "name": "Approved?", "does": "Posts if approved",
            "condition": "state.review.approved == True", "branches": {"true": "extract_tweets", "false": "declined"}}
# Naveen's plan had an `extract_json` step here; a new one is refused since F1553 (BLD4), and the list is the
# model step's own declared field, which the run holds it to. Still a feeder (`_FEEDERS`: an llm step).
EXTRACT = {"id": "extract_tweets", "type": "llm", "name": "Extract Tweet List", "does": "Lists the tweets",
           "prompt": "List the tweets.\n\n{{state.draft_tweets}}",
           "config": {"output_fields": [{"name": "tweets", "type": "list", "description": "Each tweet"}]}}
ARRAY = {"id": "tweets_array", "type": "transform", "name": "Get Tweets Array", "does": "Pulls out the list",
         "config": {"transform_code": "result = state['extract_tweets']['tweets']"}}
LOOP = {"id": "post_loop", "type": "loop", "name": "Post Each Tweet", "does": "Posts each tweet",
        "loop_over": "tweets_array", "body": ["post_tweet"]}
TWEET = {"id": "post_tweet", "type": "tool", "name": "Post Tweet", "does": "Posts one tweet", "tool": POST}
DECLINED = {"id": "declined", "type": "end", "name": "End — Not Approved", "does": "Ends without posting"}
# Naveen's plan, as event 135 of his run had it (scrape_url here is the workspace's own weather tool).
NAVEENS = (CHECK, DRAFT, REVIEW, APPROVED, EXTRACT, ARRAY, LOOP, TWEET, DECLINED)


async def _without(*steps, tools=(POST,)):
    run = await _run(plan_findings={"goal": "post tweets", "options": NO_WAY})
    draft = {"finish": "submit_plan", **_plan(*steps)}
    first, _ = await _check(run, draft)
    assert all(f"without:{t}" in first["offers"] for t in tools), first["offers"]
    run.shared_state[build_plan.ANSWER_KEY] = {"outcome": "without", "without": list(tools)}
    out, _ = await _check(run, draft)
    return out


def _ids(out) -> list[str]:
    return [s["id"] for s in out["plan"]["steps"]]


def _said(out) -> str:
    return f"{out.get('say') or ''}\n{out.get('review') or ''}"


async def test_a_loop_left_empty_goes_with_the_steps_that_only_fed_it(client):
    out = await _without(*NAVEENS)
    assert out["outcome"] in ("review", "prompts"), _said(out)
    assert _ids(out) == ["check", "draft_tweets", "review", "check_approved", "declined"]
    assert "a loop names its body steps" not in _said(out) and "post_loop" not in _said(out)
    approved = next(s for s in out["plan"]["steps"] if s["id"] == "check_approved")
    assert set(approved["branches"].values()) <= {*_ids(out), "end"}, "no arm leads to a step taken out"
    if out["outcome"] == "review":
        assert "build" in out["offers"]


async def test_the_card_says_what_went_and_why_in_the_persons_words(client):
    out = await _without(*NAVEENS)
    assert ("Without `twitter_x_post_tweet_x`, “Post Each Tweet” has nothing left to do, so I took it out, with "
            "“Extract Tweet List” and “Get Tweets Array”, which only fed it.") in out["say"]


async def test_a_parallel_with_every_lane_dropped_goes_too(client):
    lanes = {"id": "post_both", "type": "parallel", "name": "Post Everywhere", "does": "Posts on both accounts",
             "body": ["post_a", "post_b"]}
    a = {**TWEET, "id": "post_a", "name": "Post on the shop's account"}
    b = {**TWEET, "id": "post_b", "name": "Post on the café's account"}
    tell = {"id": "tell", "type": "llm", "name": "Say it is done", "does": "Says what ran", "prompt": "Say it ran."}
    out = await _without(CHECK, lanes, a, b, tell)
    assert _ids(out) == ["check", "tell"], _said(out)
    assert out["plan"]["steps"][0]["next"] == "tell"
    assert "“Post Everywhere” has nothing left to do, so I took it out" in out["say"]


async def test_a_loop_with_a_step_left_in_it_stays(client):
    note = {"id": "note", "type": "llm", "name": "Note each tweet", "does": "Notes it",
            "prompt": "Note {{state.item}}."}
    out = await _without(*NAVEENS[:6], {**LOOP, "body": ["post_tweet", "note"]}, TWEET, note, DECLINED)
    assert "post_loop" in _ids(out) and "tweets_array" in _ids(out) and "extract_tweets" in _ids(out)
    assert next(s for s in out["plan"]["steps"] if s["id"] == "post_loop")["body"] == ["note"]
    assert "has nothing left to do" not in out["say"]


async def test_a_feeder_a_kept_step_still_reads_is_kept(client):
    count = {"id": "count", "type": "llm", "name": "Count the tweets", "does": "Says how many there are",
             "prompt": "How many tweets are in {{state.tweets_array}}?"}
    out = await _without(*NAVEENS[:7], TWEET, count, DECLINED)
    assert "post_loop" not in _ids(out)
    assert "tweets_array" in _ids(out) and "extract_tweets" in _ids(out), "Count the tweets still reads it"
    assert "which only fed it" not in out["say"]


@pytest.mark.parametrize("feeder", [
    {"id": "fetch", "type": "tool", "name": "Read the shop's posts", "does": "Reads them", "tool": "rotterdam_weather"},
    {"id": "fetch", "type": "human", "name": "Ask which tweets", "does": "Asks the person", "prompt": "Which ones?"},
])
async def test_a_tool_or_a_persons_step_is_never_taken_out_as_a_feeder(client, feeder):
    out = await _without(CHECK, feeder, {**LOOP, "loop_over": "fetch"}, TWEET)
    assert "post_loop" not in _ids(out) and "fetch" in _ids(out)


async def test_a_check_the_plan_fails_names_its_steps_never_their_ids(client):
    """A check's text reaching the card: the steps by their names, never `step <id>:` or the check's own words, and
    the raw text kept in `issues` for the planner."""
    run = await _run()
    empty = {**LOOP, "body": []}
    out, _ = await _check(run, {"finish": "submit_plan", **_plan(CHECK, empty)})
    said = _said(out)
    assert "a loop names its body steps" not in said and "step post_loop" not in said and "post_loop:" not in said
    assert "“Post Each Tweet” doesn't fit together as it is" in out["say"]
    assert "Tell me what to change, or ask me to plan it again." in out["say"]
    assert any("a loop names its body steps" in i for i in out["issues"]), "kept for the planner"


async def test_a_plan_that_cannot_be_read_is_said_without_the_checks_words(client):
    run = await _run()
    out, _ = await _check(run, {"finish": "submit_plan", **_plan({**CHECK, "type": "teleport"})})
    assert out["outcome"] == "said"
    assert out["say"].startswith("I could not make a plan that holds together")
    assert "teleport" not in out["say"] and "steps." not in out["say"] and "Input should be" not in out["say"]
    assert "Tell me what to change, or ask me to plan it again." in out["say"]


async def test_a_check_already_in_the_persons_words_reaches_the_card_word_for_word(client):
    """A save rule writes for the person ("Step 'Post the update' runs `post_update`, which changes something outside
    this workspace, with nobody approving it first. Put a Human step before it…"): it names no id and says what to
    do, so it is said as it is, never turned into "doesn't fit together" (CI2's reader run at 383ec076)."""
    from core.store.resources import save_tool

    await save_tool({"name": "post_update", "description": "Posts an update outside.", "tool_type": "http",
                     "method": "POST", "url": "https://api.example.com/post", "headers": {},
                     "inputSchema": {"type": "object", "properties": {}}})
    run = await _run()
    post = {"id": "post", "type": "tool", "name": "Post the update", "does": "Posts it", "tool": "post_update"}
    out, _ = await _check(run, {"finish": "submit_plan", **_plan(CHECK, post)})
    [rule] = [i for i in out["issues"] if "Human step" in i]
    assert "Step 'Post the update'" in rule, rule
    assert rule in out["say"] and rule in out["review"], "word for word, on the card"
    assert "doesn't fit together" not in _said(out)
