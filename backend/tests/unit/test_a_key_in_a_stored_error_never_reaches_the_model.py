"""A key quoted in a run's error never reaches the model, the run page or a list (REACH1's triage run, 10-30; 8a).

A provider's refusal can quote the key it refused ("HTTP 401: invalid key sk-…"). That key was never one of the run's
held variable values, so the held-value scrub (`scrub_secrets`, F854) let it through: into the run's record, and from
there to every reader. Fixed at the two places they share, `orchestration.run_error`:

- the write: `reason_of` records a step's error with key-shaped tokens marked, so the stream, the logs and a
  channel's line never carry it;
- the read: `failure_reason` and `clean_history` clean what a run already holds, so a run recorded before reads the
  same through get_run, read_workspace's run topic, the runs list and the run page.
"""
from __future__ import annotations

import json

from core.builder_tools import execute_builder_tool_with_events
from core.models_orchestration import OrchestrationRun
from core.orchestration.state import SharedState
from core.secret_scrub import CREDENTIAL_MARK

KEY = "sk-" + "proj-" + "K" * 40
BEARER = "Bearer " + "abc123XYZ" * 4


class _Server:
    agent_sessions: dict = {}
    tool_router: dict = {}


async def _stored(run_id="run_leak", *, run_error=None):
    from core.store.resources import save_orchestration
    from tests._fakes.seed import make_orchestration

    await save_orchestration(make_orchestration(id="orch_leak", name="Leaky"))
    run = OrchestrationRun(
        run_id=run_id, orchestration_id="orch_leak", status="failed", current_step_id="s1",
        started_at="2026-10-30T10:00:00Z", ended_at="2026-10-30T10:01:00Z",
        step_history=[{"step_id": "s1", "step_name": "Call", "step_type": "agent", "status": "failed",
                       "error": f"LLMError: HTTP 401: invalid api key {KEY}"}],
        step_progress={"__run__": {"error": f"Stopped: the provider said {BEARER}"}} if run_error else {})
    await SharedState(run).checkpoint()


def _clean(text: str) -> None:
    assert KEY not in text and "abc123XYZabc123XYZ" not in text, text


def test_the_write_marks_it():
    from core.llm_providers import LLMError
    from core.orchestration.run_error import reason_of

    said = reason_of(LLMError(f"HTTP 401: invalid api key {KEY}"))
    _clean(said)
    assert CREDENTIAL_MARK in said and said.startswith("LLMError: HTTP 401")


def test_ids_stay_readable_so_triage_keeps_them():
    """Only a key's shape or label is taken (8a's read of the patterns): a run's, a step's and a request's id in an
    error are what a triage needs, and stay."""
    from core.orchestration.run_error import clean

    run_id = "run_orch_ab12cd34_1761818400000_9f3e2a"
    child = f"{run_id}__step_fetch_orders_d1_4b7c1e"
    said = clean(f"Step 'Fetch orders' (step_fetch_orders) in run {run_id} failed: HTTP 500 from the vendor, "
                 f"request id req_7Hx2kQ9vL3; child {child}; key {KEY}")
    for kept in (run_id, child, "step_fetch_orders", "req_7Hx2kQ9vL3"):
        assert kept in said, (kept, said)
    _clean(said)


def test_the_one_reader_cleans_what_a_run_already_holds():
    from core.orchestration.run_error import clean_history, failure_reason

    history = [{"step_id": "s1", "error": f"HTTP 401 {KEY}"}, {"step_id": "s2", "status": "completed"}]
    _clean(failure_reason("failed", history, {}))
    _clean(failure_reason("failed", history, {"__run__": {"error": BEARER}}))
    cleaned = clean_history(history)
    _clean(json.dumps(cleaned))
    assert cleaned[1] == history[1] and KEY in history[0]["error"], "a copy: the record itself is not rewritten"


async def test_get_run_and_the_run_topic_never_carry_it(client):
    from core.orchestration.run_view import read_run

    await _stored(run_error=True)
    _clean(json.dumps(await read_run("run_leak")))
    raw, _ = await execute_builder_tool_with_events("read_workspace", {"topic": "run", "run_id": "run_leak"},
                                                    _Server())
    _clean(raw)
    said = json.dumps(json.loads(raw), ensure_ascii=False)
    assert CREDENTIAL_MARK in said, "what was taken out is marked, not silently dropped"


async def test_the_runs_list_and_the_today_topic_never_carry_it(client):
    from core.orchestration.run_view import list_runs

    await _stored()
    _clean(json.dumps(await list_runs(limit=10)))
    for topic in ("runs", "today"):
        raw, _ = await execute_builder_tool_with_events("read_workspace", {"topic": topic, "days": 30}, _Server())
        _clean(raw)


async def test_the_run_page_never_shows_it(client):
    await _stored(run_error=True)
    page = await client.get("/api/orchestrations/runs/run_leak")
    assert page.status_code == 200, page.text
    _clean(page.text)
