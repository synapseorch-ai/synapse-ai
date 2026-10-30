"""Synapse AI triages a failed run and resumes it once fixed (F1260, REACH1; D340 R1, Naveen 10-30: "see logs to
triage something").

A run's view said that it failed and its last error; the planner guessed the rest from a sentence. Now code reads it:

- `triage.classify` types a failure from what the run recorded (a model failure's code, the run's cap, its turn
  limit, a time limit, a tool's HTTP status, a route, a stop), never from a model's reading, and each class names
  the change ops that fix it;
- `triage.failure` says the failing step, its agent and model, the error (secrets scrubbed), whether it can resume,
  and what the step last did within one vault read; a saved output is its excerpt and key, never loaded whole;
- topic `run` carries it, and with nothing named reads the latest failed run;
- `replay` is the one place a resume is decided: the run page's Replay and Synapse AI's card both come through it,
  in the route's words; `resume_run` is the app's only;
- the resume card says the step it runs on from, and is refused for a run still going.
"""
from __future__ import annotations

import datetime as dt
import json

import pytest

from core import builder_access
from core.builder_tools import execute_builder_tool_with_events
from core.models_orchestration import OrchestrationRun
from core.orchestration import replay, triage
from core.orchestration.state import SharedState
from core.product_tools import confirm, hooks

KEY = "sk-" + "proj-" + "Q" * 40


class _Server:
    agent_sessions: dict = {}
    tool_router: dict = {}


@pytest.fixture(autouse=True)
def fresh():
    confirm.set_claim_store(None)
    builder_access.forget_runs()
    yield
    confirm.set_claim_store(None)
    hooks.set_role_source(None)
    builder_access.forget_runs()


def _ago(**delta) -> str:
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(**delta)).strftime("%Y-%m-%dT%H:%M:%SZ")


async def _agent():
    from core.store.resources import save_agent
    from tests._fakes.seed import make_agent

    agent = make_agent(name="Poster", model="claude-haiku-4-5")
    await save_agent(agent)
    return agent


async def _failed(run_id="run_bad", *, entry=None, progress=None, state=None, status="failed", agent_id="a1",
                  current="post", started=None):
    from core.store.resources import save_orchestration
    from tests._fakes.seed import make_orchestration

    definition = {"id": "orch_post", "name": "Weekly post", "entry_step_id": "post",
                  "steps": [{"id": "post", "name": "Write the post", "type": "agent", "agent_id": agent_id,
                             "output_key": "post"}]}
    await save_orchestration(make_orchestration(id="orch_post", name="Weekly post", entry_step_id="post",
                                                steps=definition["steps"]))
    run = OrchestrationRun(
        run_id=run_id, orchestration_id="orch_post", status=status, started_at=started or _ago(hours=1),
        ended_at=_ago(minutes=50), current_step_id=current, definition=definition, shared_state=state or {},
        step_history=[{"step_id": "post", "step_name": "Write the post", "step_type": "agent", "status": "failed",
                       **(entry or {"error": "Something broke"})}],
        step_progress=progress or {})
    await SharedState(run).checkpoint()
    return run


async def _read(**args) -> dict:
    raw, _ = await execute_builder_tool_with_events("read_workspace", {"topic": "run", **args}, _Server())
    return json.loads(raw)


class TestTheClass:
    @pytest.mark.parametrize("code, kind", [("quota_exhausted", "model_quota"), ("provider_refused", "model_refused"),
                                            ("rate_limited", "model_busy"), ("provider_unavailable", "model_busy"),
                                            ("model_no_answer", "model_no_answer"), ("model_error", "model_error")])
    def test_a_model_failure_is_its_code(self, code, kind):
        assert triage.classify("LLMError: anything", {"model_failure": {"code": code}}, status="failed") == (kind, None)

    @pytest.mark.parametrize("error, kind, http", [
        ("Stopped: this run reached its cap of 5 credits.", "run_cap", None),
        ("Global turn limit (100) exceeded", "turn_limit", None),
        ("Step 'Fetch' timed out after 10 minutes", "time_limit", None),
        ("ToolCallFailed: HTTP 401: invalid token", "tool_auth", 401),
        ("ToolCallFailed: HTTP 503: unavailable", "tool_down", 503),
        ("ToolCallFailed: HTTP 422: missing field 'email'", "tool_input", 422),
        ("ToolCallFailed: the tool said no", "tool", None),
        ("Something nobody named", "other", None),
    ])
    def test_the_run_s_own_words_decide_the_rest(self, error, kind, http):
        assert triage.classify(error, {}, status="failed") == (kind, http)

    def test_a_stop_by_a_person_is_its_own(self):
        assert triage.classify("", {}, status="cancelled") == ("stopped", None)

    def test_every_class_names_its_fixes_and_says_what_it_means(self):
        assert set(triage.FIXES) == set(triage.MEANS)
        from core.native_builder import change_ops as C

        named = {op for ops in triage.FIXES.values() for op in ops}
        # Each fix is an op of the one table, or another lane's op named for when it lands (F1269: reconnect).
        assert named - set(C.OPS) <= {"app_reconnect"}


class TestTheFailure:
    async def test_it_says_the_step_its_agent_the_error_and_the_fix(self, client):
        agent = await _agent()
        await _failed(agent_id=agent["id"], entry={"error": f"LLMError: HTTP 401 with key {KEY}",
                                                   "model_failure": {"code": "provider_refused"}})
        got = await triage.failure("run_bad")
        assert got["step"] == {"id": "post", "name": "Write the post", "type": "agent", "agent": "Poster",
                               "model": "claude-haiku-4-5"}
        assert got["class"] == "model_refused" and got["fixes"] == ["set_agent_model"]
        assert got["model_failure"] == "provider_refused" and KEY not in json.dumps(got)
        assert got["resumes"] is True

    async def test_what_it_last_did_is_one_bounded_read(self, client):
        from core import vault

        transcript = "\n".join(f"turn {i}: called fetch_page and read {'x' * 300}" for i in range(400))
        await _failed(progress={"post": {"context_text": transcript, "tools_used": ["fetch_page"] * 30}})
        got = await triage.failure("run_bad")
        assert len(got["last_said"]) <= vault.read_cap() + 1
        assert got["last_said"].rstrip().endswith(transcript.rstrip()[-40:])
        assert len(got["tools_used"]) == triage.TOOLS

    async def test_a_saved_output_is_its_excerpt_and_key(self, client):
        from core import step_refs

        big = "A long post. " * 5000
        ref = step_refs.save("run_bad", "post", big, name="post")
        await _failed(state={"post": ref})
        got = await triage.failure("run_bad")
        assert ref[step_refs.REF] in got["its_output"] and len(got["its_output"]) < len(big)

    async def test_a_run_that_never_reached_a_step_cannot_resume(self, client):
        await _failed(current=None)
        got = await triage.failure("run_bad")
        assert got["resumes"] is False and got["resume_refused"] == replay.NO_STEP

    async def test_a_finished_run_has_none(self, client):
        await _failed(status="completed")
        assert await triage.failure("run_bad") is None


class TestTheTopic:
    async def test_a_failed_run_carries_its_failure(self, client):
        await _failed(entry={"error": "Global turn limit (100) exceeded"})
        got = await _read(run_id="run_bad")
        assert got["failure"]["class"] == "turn_limit" and got["failure"]["fixes"] == ["update_workflow"]

    async def test_nothing_named_reads_the_latest_failed_run(self, client):
        await _failed("run_older", started=_ago(days=2), entry={"error": "Old"})
        await _failed("run_newer", started=_ago(minutes=5), entry={"error": "Global turn limit (100) exceeded"})
        got = await _read()
        assert got["run_id"] == "run_newer" and got["failure"]["class"] == "turn_limit"

    async def test_none_failed_is_said(self, client):
        got = await _read()
        assert got["error"].startswith("No workflow here has a failed run.")


class TestResuming:
    def test_the_refusals_are_the_run_pages_words(self):
        assert replay.refusal("run_a__step", "failed", "s1") == replay.NESTED
        assert replay.refusal("run_a", "completed", "s1") == (
            "The run is completed; only a failed or cancelled run can be replayed.")
        assert replay.refusal("run_a", "failed", None) == replay.NO_STEP
        assert replay.refusal("run_a", "cancelled", "s1") is None

    async def test_the_route_still_refuses_a_nested_run_in_its_words(self, client):
        refused = await client.post("/api/orchestrations/runs/run_a__child/resume")
        assert refused.status_code == 409 and refused.json()["detail"] == replay.NESTED

    async def _card(self, run_id):
        await SharedState(OrchestrationRun(run_id="builder_tri", orchestration_id="orch_native_builder",
                                           shared_state={builder_access.CALLER_KEY: {"user_id": None}})).checkpoint()
        raw, events = await execute_builder_tool_with_events("prepare_action", {"action": "resume_run",
                                                                                "run_id": run_id},
                                                             _Server(), run_id="builder_tri")
        return json.loads(raw), [e for e in events if e.get("type") == "action_needed"]

    async def test_the_card_says_the_step_it_runs_on_from(self, client):
        await _failed()
        said, [card] = await self._card("run_bad")
        assert card["title"] == 'Resume the run of "Weekly post"'
        assert 'From the step "Write the post".' in card["detail"]
        [button] = card["buttons"]
        assert button["tool"] == "resume_run" and button["arguments"] == {"run_id": "run_bad"}

    async def test_a_run_still_going_is_refused_in_its_words(self, client):
        await _failed(status="running")
        said, cards = await self._card("run_bad")
        assert cards == [] and "only a failed or cancelled run can be replayed" in said["error"]

    async def test_the_press_resumes_it_through_the_one_place(self, client, monkeypatch):
        await _failed()
        _, [card] = await self._card("run_bad")
        resumed = []

        async def resume(app_state, run_id, server_module):
            resumed.append(run_id)
            return replay.Resumed(run_id, queue=object())

        monkeypatch.setattr(replay, "resume", resume)
        done = await client.post("/api/assistant/act", json=card["buttons"][0])
        assert done.status_code == 200, done.text
        assert resumed == ["run_bad"]


def test_resume_run_is_the_apps_only():
    from core.product_tools import areas, registry
    from core.product_tools.caller import Caller
    from core.product_tools.roles import Role

    areas.ensure_loaded()
    tool = registry.get("resume_run")
    assert tool.app_only and tool.min_role == Role.BUILDER
    assert not tool.offered_to(Caller(role=Role.OWNER, via="mcp"))
