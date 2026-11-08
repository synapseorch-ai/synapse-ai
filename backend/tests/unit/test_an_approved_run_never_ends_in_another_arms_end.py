"""
A step with no `next` never ends the run in an End another arm names (F1429, BLD1; L01 in the 2.0.1 list, pulled into
v2.0; Naveen's scratch, 11-06, run builder_77a81e4bb602).

Naveen's plan drafts tweets, asks for a yes, then posts each in a loop; its one End is "End — Not Approved", the
false arm's. The loop had no `next`, and `settled()` sent it to the next step in the list, that End, so an approved
run ended on "End — Not Approved"; after "build without" took the posting out, the approved arm went straight there.
An End only ends the run (EndStepExecutor), so nothing approved went undone by the route: what was wrong is the End
the run shows. Now, as the engine reads a step with no next (the run ends):

- a step whose next listed step is an End some arm names (that arm's own End) goes to a plain End instead;
- the compiled plan's "end" is an End no arm names, and one "End" is added when every End is an arm's;
- a saved workflow read back keeps which End each arm goes to.

A list still says the order where nothing else does: a linear plan, the merge shape (true → B, false → C; B then C)
and a trailing End no arm names are as they were. A step that falls into another arm's first step that is not an
End looks like the merge shape in code, so it is left for 2.0.1.
"""
from __future__ import annotations

from core.native_builder import build_plan
from core.native_builder import objects as O
from core.native_builder.compile import compile_plan, plan_from_workflow

POST = "twitter_x_post_tweet_x"
DRAFT = {"id": "draft_tweets", "type": "llm", "name": "Draft Tweets", "does": "Writes 5 tweets",
         "prompt": "Write 5 tweets about the shop."}
REVIEW = {"id": "review", "type": "human", "name": "Review Tweets", "does": "Asks for a yes before posting",
          "prompt": "Post these?\n\n{{state.draft_tweets}}"}
APPROVED = {"id": "check_approved", "type": "if_else", "name": "Approved?", "does": "Posts if approved",
            "condition": "state.review.approved == True", "branches": {"true": "extract_tweets", "false": "declined"}}
EXTRACT = {"id": "extract_tweets", "type": "extract_json", "name": "Extract Tweet List", "does": "Parses the tweets",
           "prompt": "Extract the tweets as {\"tweets\": [...]}\n\n{{state.draft_tweets}}"}
ARRAY = {"id": "tweets_array", "type": "transform", "name": "Get Tweets Array", "does": "Pulls out the list",
         "config": {"transform_code": "result = state['extract_tweets']['tweets']"}}
LOOP = {"id": "post_loop", "type": "loop", "name": "Post Each Tweet", "does": "Posts each tweet",
        "loop_over": "tweets_array", "body": ["post_tweet"]}
TWEET = {"id": "post_tweet", "type": "tool", "name": "Post Tweet", "does": "Posts one tweet", "tool": POST}
DECLINED = {"id": "declined", "type": "end", "name": "End — Not Approved", "does": "Ends without posting"}
# Naveen's plan, as event 135 of his run had it, the weather check left out.
NAVEENS = (DRAFT, REVIEW, APPROVED, EXTRACT, ARRAY, LOOP, TWEET, DECLINED)


def _plan(*steps) -> O.Plan:
    plan, errors = O.read({"kind": "create", "title": "Shop tweets", "summary": "Post tweets about the shop.",
                           "steps": list(steps)})
    assert plan is not None, errors
    return plan


def _settled(*steps) -> dict[str, O.Step]:
    return {s.id: s for s in O.settled(_plan(*steps).steps)}


def _compiled(plan: O.Plan) -> dict[str, dict]:
    return {s["id"]: s for s in compile_plan(plan).orchestration["steps"]}


def _ends(steps: dict[str, dict]) -> dict[str, str]:
    return {i: s["name"] for i, s in steps.items() if s["type"] == "end"}


def test_the_approved_arm_does_not_fall_into_the_declined_end():
    steps = _settled(*NAVEENS)
    assert steps["post_loop"].next == "end", "the loop is the approved arm's last step: the run ends there"
    assert steps["check_approved"].branches == {"true": "extract_tweets", "false": "declined"}


def test_the_compiled_approved_arm_ends_on_a_plain_end():
    steps = _compiled(_plan(*NAVEENS))
    ends = _ends(steps)
    plain = steps["post_loop"]["next_step_id"]
    assert plain != "declined" and ends.get(plain) == "End", (plain, ends)
    assert steps["check_approved"]["if_false_step_id"] == "declined", "the declined arm still ends where it said"


def test_after_build_without_the_approved_arm_ends_on_a_plain_end():
    plan = build_plan._without(_plan(*NAVEENS), [POST])
    steps = _compiled(plan)
    ends = _ends(steps)
    approved = steps["check_approved"]
    assert approved["if_true_step_id"] != "declined" and ends.get(approved["if_true_step_id"]) == "End", (
        approved["if_true_step_id"], ends)
    assert approved["if_false_step_id"] == "declined"


A = {"id": "a", "type": "llm", "name": "A", "does": "a", "prompt": "a"}
B = {"id": "b", "type": "llm", "name": "B", "does": "b", "prompt": "b"}
C = {"id": "c", "type": "llm", "name": "C", "does": "c", "prompt": "c"}
SPLIT = {"id": "split", "type": "if_else", "name": "Split", "does": "splits", "condition": "state.a == 'x'",
         "branches": {"true": "b", "false": "c"}}
DONE = {"id": "done", "type": "end", "name": "Done", "does": "ends"}


def test_a_linear_plan_still_goes_in_its_order():
    steps = _settled(A, B, C)
    assert (steps["a"].next, steps["b"].next, steps["c"].next) == ("b", "c", "end")


def test_the_merge_shape_still_falls_into_the_step_both_arms_reach():
    """true → B, false → C, with B listed before C: B goes on to C, as it did."""
    steps = _settled(A, SPLIT, B, C)
    assert steps["a"].next == "split" and steps["b"].next == "c"


def test_a_trailing_end_no_arm_names_is_still_where_the_run_ends():
    steps = _compiled(_plan(A, B, DONE))
    assert steps["b"]["next_step_id"] == "done"
    assert list(_ends(steps)) == ["done"], "no second End is added"


def test_a_done_shaped_single_end_gains_a_plain_end_and_ends_at_the_same_point():
    """true → B, false → Done, with B listed before Done: B falls into the false arm's End, so it ends on a plain End
    (eb, 11-06: the run ends where it did, and the canvas reads right)."""
    steps = _compiled(_plan(A, {**SPLIT, "branches": {"true": "b", "false": "done"}}, B, DONE))
    ends = _ends(steps)
    assert steps["split"]["if_false_step_id"] == "done"
    assert steps["b"]["next_step_id"] != "done" and ends.get(steps["b"]["next_step_id"]) == "End", ends


def _saved(step_id: str, kind: str, name: str, **more) -> dict:
    return {"id": step_id, "type": kind, "name": name, **more}


def test_a_saved_workflow_read_back_keeps_which_end_each_arm_goes_to():
    """A workflow saved with two Ends (the declined one listed first) reads back and compiles to the same Ends: an
    update never sends the approved arm to the declined End."""
    orch = {"id": "orch_two_ends", "name": "Shop tweets", "entry_step_id": "review", "steps": [
        _saved("review", "human", "Review Tweets", human_prompt="Post these?", next_step_id="check_approved",
               output_key="review"),
        _saved("check_approved", "if_else", "Approved?", if_condition="state.review.approved == True",
               if_true_step_id="post", if_false_step_id="declined"),
        _saved("declined", "end", "End — Not Approved"),
        _saved("post", "llm", "Post", prompt_template="Post them.", next_step_id="done", output_key="post"),
        _saved("done", "end", "Done"),
    ]}
    steps = _compiled(plan_from_workflow(orch))
    assert steps["post"]["next_step_id"] == "done"
    assert steps["check_approved"]["if_false_step_id"] == "declined"


def test_a_plans_own_end_read_back_is_still_the_plain_end():
    """bld-07's shape (P0, synapse.nova-pro): no End steps, the false arm and the last step say "end". Compiled, the
    End it gets has the id `end`; read back, the arm's `end` is still the plain end, not an arm's own End, so
    compiling it again changes nothing (CI2's FF25 suite)."""
    check = {"id": "check_website", "type": "llm", "name": "Check Website", "does": "checks", "prompt": "check",
             "next": "evaluate_status"}
    split = {"id": "evaluate_status", "type": "if_else", "name": "Evaluate Status", "does": "splits",
             "condition": "state.check_website == 'down'", "branches": {"true": "send_alert", "false": "end"}}
    alert = {"id": "send_alert", "type": "llm", "name": "Send Alert", "does": "alerts", "prompt": "alert",
             "next": "end"}
    once = compile_plan(_plan(check, split, alert)).orchestration
    again = compile_plan(plan_from_workflow(once), orch_id=once["id"]).orchestration

    def routes(orch):
        return {s["id"]: (s["type"], s.get("next_step_id"), s.get("if_true_step_id"), s.get("if_false_step_id"))
                for s in orch["steps"]}

    assert routes(again) == routes(once)
    assert routes(once)["send_alert"][1] == routes(once)["evaluate_status"][3] == "end"
