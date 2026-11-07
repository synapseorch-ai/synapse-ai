"""F1441 (prod, cloud-v2.0.0-rc6, Claude Haiku 4.5 on Bedrock): a plan sent inside a wrapper key is read as the plan.

`check_draft` takes its plan under `draft`; `submit_plan` takes the plan's own fields. A model carries the one shape over
to the other: on a new org's first request Haiku sent submit_plan `{"draft": {...}}` four times, each sent back with
only "kind: Field required, title: …, summary: …", and the build stopped before anything was saved.

* `submit_plan`'s arguments that hold no plan field at their top level, and one object (or a JSON string of one) that
  holds a plan, are that plan, whatever the key is called; any other key beside it (its questions) is kept.
* `check_draft` given a plan where its `draft` should be reads that plan, and a draft wrapped once more is read too.
* Fields split between the top level and a wrapper are not guessed at: the sendback says in plain words where they
  go, before the fields it lacks.
"""
from __future__ import annotations

import json

import pytest

from core import builder_access, builder_tools  # noqa: F401 — the builder's modules, loaded as at boot
from core.builder_tools import execute_builder_tool_with_events
from core.models_orchestration import OrchestrationRun
from core.orchestration.state import SharedState


class _Server:
    agent_sessions: dict = {}
    tool_router: dict = {}


@pytest.fixture(autouse=True)
async def desk(client):
    builder_access.forget_runs()
    yield
    builder_access.forget_runs()


#: The plan as prod's Haiku wrote it (F1441), inside `draft`: its steps have no `name` or `does`, which is the planner's
#: to fix once the plan is read.
AS_SENT = {"kind": "create", "title": "Read and Summarize Webpage",
           "summary": "Reads a webpage and writes a short summary of it.",
           "steps": [{"id": "fetch_page", "type": "tool", "tool": "scrape_url",
                      "config": {"tool_arguments": {"url": "https://example.com"}}, "next": "summarize_content"},
                     {"id": "summarize_content", "type": "llm",
                      "prompt": "Summarize this page in a few sentences: {{ state.fetch_page }}", "next": "end"},
                     {"id": "end", "type": "end"}]}
#: The same plan with its steps named, as the planner sends it again.
PLAN = {**AS_SENT, "steps": [{**s, "name": n, "does": n} for s, n in zip(
    AS_SENT["steps"], ("Fetch the page", "Summarize it", "End"))]}
TOP_LEVEL = "The plan's fields go at the top level of submit_plan's arguments, not inside `draft`"

_n = 0


async def _call(tool: str, args):
    """`tool` called on a run of its own, as the planner calls it."""
    from core.orchestration.engine import live

    global _n
    _n += 1
    run = OrchestrationRun(run_id=f"builder_f1441_{_n}", orchestration_id="orch_native_builder",
                           shared_state={builder_access.CALLER_KEY: {"user_id": "u1"}, "intent": "create",
                                         "user_message": "Read https://example.com and summarise it."})
    await SharedState(run).checkpoint()
    with live(run):
        raw, _ = await execute_builder_tool_with_events(tool, args, _Server(), run_id=run.run_id)
    return json.loads(raw) if isinstance(raw, str) else raw


def _said(out) -> str:
    return json.dumps(out, ensure_ascii=False)


class TestSubmitPlan:
    async def test_prods_plan_inside_draft_is_read_as_the_plan(self, client):
        bare = await _call("submit_plan", AS_SENT)
        wrapped = await _call("submit_plan", {"draft": AS_SENT})
        assert wrapped == bare, "the same plan, sent inside `draft`"
        assert not any(f"{k}: Field required" in wrapped["error"] for k in ("kind", "title", "summary")), wrapped

    async def test_a_whole_plan_inside_draft_goes_on_as_the_plan(self, client):
        bare = await _call("submit_plan", PLAN)
        wrapped = await _call("submit_plan", {"draft": PLAN})
        assert "Field required" not in _said(bare), bare
        assert wrapped == bare, "the same plan, sent inside `draft`"

    @pytest.mark.parametrize("args", [{"plan": PLAN}, {"plan_draft": PLAN}, {"draft": json.dumps(PLAN)},
                                      json.dumps({"draft": PLAN})])
    async def test_any_wrapper_key_and_a_json_string_read_the_same(self, client, args):
        assert await _call("submit_plan", args) == await _call("submit_plan", PLAN)

    async def test_what_is_sent_beside_the_wrapper_is_kept(self, client):
        asked = ["Which page should it read?"]
        bare = await _call("submit_plan", {**PLAN, "questions": asked})
        assert await _call("submit_plan", {"draft": PLAN, "questions": asked}) == bare

    async def test_fields_split_across_the_wrapper_are_said_where_they_go(self, client):
        out = await _call("submit_plan", {"kind": "create", "draft": {k: v for k, v in PLAN.items() if k != "kind"}})
        said = out["error"]
        assert TOP_LEVEL in said and said.index(TOP_LEVEL) < said.index("Field required"), said

    async def test_a_wrapper_with_no_plan_in_it_is_sent_back_as_before(self, client):
        out = await _call("submit_plan", {"draft": {"note": "a page summary"}})
        assert "kind: Field required" in out["error"] and TOP_LEVEL not in out["error"], out


class TestCheckDraft:
    async def test_a_bare_plan_is_checked_as_its_draft(self, client):
        wrapped = await _call("check_draft", {"draft": PLAN})
        assert "Field required" not in _said(wrapped), wrapped
        assert await _call("check_draft", PLAN) == wrapped

    async def test_a_draft_wrapped_once_more_is_read(self, client):
        assert await _call("check_draft", {"draft": {"draft": PLAN}}) == await _call("check_draft", {"draft": PLAN})
