"""Synapse AI reaches what only a hosted workspace has: its dead jobs, who changed what, and support (F1260, REACH1).

Naveen, 10-30: on Synapse Cloud, Synapse AI retries or discards a job the fleet gave up on (his Q1: discard too),
answers "who changed it" (his Q2: an admin's, on a plan with the audit log) and files a support request. A self-hosted
install has no dead-letter queue, no audit log and no support desk, so the engine holds each topic, act and card, and
says plainly where there is none; a host that has them reads and does each its own way (cloud's readers and ACTS).

- Self-hosted: topics dead_jobs and audit say there are none; the cards are never offered, and say so; the three
  tools are never listed, and refuse if called;
- with a host: a dead job's retry and discard cards name its workflow and why it stopped, their buttons the host's
  tools by the job's id; one that can't be retried says why; the support card shows the whole request first, with
  its category (a question unless said), its severity (the person's, asked, never guessed) and the run it is about;
- the ops are the change table's; support_request is named by its subject; the one planner's rule 21 names them, and
  both topic lists name dead_jobs and audit.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from core import builder_access, builder_readers
from core.builder_tools import execute_builder_tool_with_events
from core.models_orchestration import OrchestrationRun
from core.orchestration.state import SharedState
from core.product_tools import confirm, hooks, operations

PLANNER = Path(__file__).resolve().parents[2] / "core" / "native_builder" / "agents" / "planner.json"


class _Server:
    agent_sessions: dict = {}
    tool_router: dict = {}


@pytest.fixture(autouse=True)
def fresh():
    confirm.set_claim_store(None)
    builder_access.forget_runs()
    yield
    operations.set_available(False)
    builder_readers.set_workspace_reader("dead_jobs", None)
    confirm.set_claim_store(None)
    hooks.set_role_source(None)
    builder_access.forget_runs()


async def _prepare(args):
    await SharedState(OrchestrationRun(run_id="builder_ops", orchestration_id="orch_native_builder",
                                       shared_state={builder_access.CALLER_KEY: {"user_id": None}})).checkpoint()
    raw, events = await execute_builder_tool_with_events("prepare_action", args, _Server(), run_id="builder_ops")
    return json.loads(raw), [e for e in events if e.get("type") == "action_needed"]


async def _read(topic: str) -> dict:
    raw, _ = await execute_builder_tool_with_events("read_workspace", {"topic": topic}, _Server())
    return json.loads(raw)


@pytest.fixture
def a_host():
    jobs = [{"id": "dj_1", "workflow": "Invoice chaser", "run_id": "run_a", "failed_at": "2026-11-01T10:00:00Z",
             "why": "the worker was lost mid-step", "can_retry": True},
            {"id": "dj_2", "workflow": "Weekly digest", "run_id": "run_b", "failed_at": "2026-11-01T11:00:00Z",
             "why": "its step data was cleared", "can_retry": False,
             "why_not": "Its step data is cleared, so there is nothing to resume from."}]

    async def reader(args):
        return {"items": jobs, "note": ""}

    operations.set_available(True)
    builder_readers.set_workspace_reader("dead_jobs", reader)
    return jobs


class TestSelfHosted:
    async def test_the_topics_say_there_are_none(self, client):
        dead = await _read("dead_jobs")
        assert dead["items"] == [] and dead["note"] == operations.NO_DEAD_JOBS
        audit = await _read("audit")
        assert audit["items"] == [] and "keeps no record of who changed what" in audit["note"]

    @pytest.mark.parametrize("args, said", [
        ({"action": "dead_job_retry", "job_id": "dj_1"}, operations.NO_DEAD_JOBS),
        ({"action": "dead_job_discard", "job_id": "dj_1"}, operations.NO_DEAD_JOBS),
        ({"action": "support_request", "name": "Runs fail", "text": "Every run fails at step 2."}, operations.NO_SUPPORT),
    ])
    async def test_the_cards_are_never_offered_and_say_so(self, client, args, said):
        got, cards = await _prepare(args)
        assert cards == [] and got["error"] == said

    async def test_the_tools_are_never_listed_and_refuse_if_called(self, client):
        from core.product_tools import areas, registry
        from core.product_tools.caller import Caller
        from core.product_tools.errors import ToolRefusal
        from core.product_tools.roles import Role

        areas.ensure_loaded()
        owner = Caller(role=Role.OWNER, via="assistant")
        for name in ("retry_dead_job", "discard_dead_job", "file_support_request"):
            tool = registry.get(name)
            assert tool.app_only and not tool.offered_to(owner)
            with pytest.raises(ToolRefusal):
                await tool.handler(owner, {})


class TestWithAHost:
    async def test_a_retry_card_names_the_job_and_presses_the_hosts_tool(self, client, a_host):
        _, [card] = await _prepare({"action": "dead_job_retry", "job_id": "dj_1"})
        assert card["title"] == 'Retry the job that failed in "Invoice chaser"'
        assert card["detail"].startswith("It stopped: the worker was lost mid-step\n")
        [button] = card["buttons"]
        assert button["tool"] == "retry_dead_job" and button["arguments"] == {"job_id": "dj_1"}

    async def test_one_that_cant_be_retried_says_why_and_can_be_discarded(self, client, a_host):
        got, cards = await _prepare({"action": "dead_job_retry", "job_id": "dj_2"})
        assert cards == [] and got["error"] == a_host[1]["why_not"]
        _, [card] = await _prepare({"action": "dead_job_discard", "job_id": "dj_2"})
        assert card["danger"] and card["title"] == 'Discard the job that failed in "Weekly digest"'
        assert card["buttons"][0]["tool"] == "discard_dead_job"

    async def test_the_support_card_shows_the_whole_request_first(self, client, a_host):
        _, [card] = await _prepare({"action": "support_request", "name": "Runs stop at step 2",
                                    "text": "Since this morning every run of Invoice chaser stops at Send.",
                                    "severity": "degraded", "run_id": "run_a"})
        assert card["title"] == 'Send support "Runs stop at step 2"'
        assert card["detail"] == ("Since this morning every run of Invoice chaser stops at Send.\n"
                                  "Category: question. Severity: degraded.\nAbout the run run_a.")
        [button] = card["buttons"]
        assert button["tool"] == "file_support_request"
        assert button["arguments"] == {"subject": "Runs stop at step 2", "category": "question",
                                       "body": "Since this morning every run of Invoice chaser stops at Send.",
                                       "severity": "degraded", "run_id": "run_a"}

    async def test_a_request_with_nothing_said_is_asked_for(self, client, a_host):
        got, cards = await _prepare({"action": "support_request", "name": "Help"})
        assert cards == [] and "what happened" in got["error"]

    async def test_how_bad_it_is_is_the_persons_never_guessed(self, client, a_host):
        """The card can't be edited, so its severity is asked, never defaulted (TRI1, D0: what only they know)."""
        got, cards = await _prepare({"action": "support_request", "name": "Runs stop", "text": "Every run stops."})
        assert cards == [] and "blocking, degraded or minor" in got["error"]


def test_the_ops_and_the_planner_name_them():
    from core.builder_actions import ACTIONS
    from core.native_builder import change_ops as C
    from core.native_builder.answer_pack import ACT_TOOLS

    for op, tool in (("dead_job_retry", "retry_dead_job"), ("dead_job_discard", "discard_dead_job"),
                     ("support_request", "file_support_request")):
        assert C.OPS[op].is_act and op in ACTIONS and ACT_TOOLS[op] == tool
    assert "support_request" in C.MAKES_TARGET
    assert C.act_arguments(C.OPS["dead_job_retry"], "dj_1", {}) == {"action": "dead_job_retry", "job_id": "dj_1"}

    prompt = json.loads(PLANNER.read_text("utf-8"))["system_prompt"]
    rule = re.search(r"\n21\. (.*?)(?=\n\d+\. |\n\n)", prompt, re.S)
    assert rule and all(op in rule.group(1) for op in ("dead_job_retry", "dead_job_discard", "support_request"))
    # A run only when the request is about one; how bad it is asked of the person, never guessed (TRI1).
    assert "the `run_id` when it is about one" in rule.group(1) and "`ask_person`" in rule.group(1)
    for listed in re.findall(r"answers by topic \(([^)]*)\)", prompt):
        assert {"dead_jobs", "audit"} <= {t.strip() for t in listed.split(",")}
