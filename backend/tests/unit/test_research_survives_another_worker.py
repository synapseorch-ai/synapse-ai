"""What a research has read, and how much it has searched, is kept on the run, not in one process (F777; SAI2-P1,
BWA1's review point 1).

`report_findings` keeps only verdicts the research's own reading supports (F577/F667): a provider's own MCP server
the Registry showed, the hosts whose pages were read. F765's backstop counts each kind of call from a research's
first to its report. Both lived in a module dict keyed by run id, so a run handed to another worker — a scale-down,
a deploy — lost them: every verdict became `unclear`, and the backstop started again.

They are now the run's own record, `step_progress["__run__"]["research"]` (the entry both products already store and
checkpoint), written through as each call is counted. A worker that resumes the run reads them back. A person's
Replay starts afresh, as the run's time budget does (F258); an answer to a question, or a hand-off, does not.
"""
import json

import pytest

from core import builder_research as research
from core.builder_tools import _current_run
from core.models_orchestration import OrchestrationRun
from core.orchestration import engine as engine_mod
from core.orchestration.state import SharedState


@pytest.fixture(autouse=True)
async def _store_ready():
    from core.store import get_store

    await get_store()
    yield


async def _live_run(run_id: str) -> OrchestrationRun:
    run = OrchestrationRun(run_id=run_id, orchestration_id="orch_native_builder", status="running")
    await SharedState(run).checkpoint()
    return run


async def _other_worker(run_id: str) -> OrchestrationRun:
    """What another process has: nothing in memory, the run as the store keeps it."""
    research._LOOSE.clear()
    return (await SharedState.restore(run_id)).run


class TestTheBackstopCountsAcrossWorkers:
    async def test_the_count_is_the_runs(self):
        run = await _live_run("builder_f777_a")
        token = _current_run.set(run.run_id)
        try:
            with engine_mod.live(run):
                for _ in range(research.BACKSTOP["search_the_web"]):
                    assert await research._over_backstop("search_the_web") is False
            moved = await _other_worker(run.run_id)
            with engine_mod.live(moved):
                assert await research._over_backstop("search_the_web") is True, "the 13th search, after a move"
        finally:
            _current_run.reset(token)


class TestTheVerdictsSurviveAMove:
    async def test_a_providers_own_server_and_the_pages_read_are_still_known(self):
        run = await _live_run("builder_f777_b")
        token = _current_run.set(run.run_id)
        try:
            with engine_mod.live(run):
                await research._saw_own_server("Mailchimp", "https://registry.example/mailchimp")
                await research._read_host("https://mailchimp.com/developer/")
            moved = await _other_worker(run.run_id)
            with engine_mod.live(moved):
                out = await research.report_findings({"goal": "send the newsletter", "options": [
                    {"provider": "Mailchimp", "how": "mcp", "detail": "their own server",
                     "sources": ["https://mailchimp.com/developer/"]},
                    {"provider": "Mailchimp", "how": "api", "detail": "REST",
                     "sources": ["https://mailchimp.com/developer/"]}]}, None, [])
            assert [o["how"] for o in out["options"]] == ["mcp", "api"]
            assert "research" not in (moved.step_progress.get(engine_mod.ACTIVE_TIME_KEY) or {}), "spent by the report"
        finally:
            _current_run.reset(token)

    async def test_without_its_reading_a_verdict_is_unclear_as_before(self):
        run = await _live_run("builder_f777_c")
        token = _current_run.set(run.run_id)
        try:
            with engine_mod.live(run):
                out = await research.report_findings({"options": [
                    {"provider": "Mailchimp", "how": "mcp", "sources": ["https://mailchimp.com/"]}]}, None, [])
            assert out["options"][0]["how"] == "unclear"
        finally:
            _current_run.reset(token)


class TestWhereTheRecordLives:
    async def test_it_is_on_the_runs_own_entry_and_checkpointed(self):
        run = await _live_run("builder_f777_d")
        token = _current_run.set(run.run_id)
        try:
            with engine_mod.live(run):
                await research._over_backstop("read_web_page")
                await research._read_host("https://docs.example.com/a")
            stored = (await SharedState.restore(run.run_id)).run.step_progress[engine_mod.ACTIVE_TIME_KEY]["research"]
            assert stored["spent"] == {"read_web_page": 1} and stored["read_hosts"] == ["docs.example.com"]
            json.dumps(stored)  # a stored record is plain JSON
        finally:
            _current_run.reset(token)

    async def test_outside_a_run_it_is_kept_in_memory_as_before(self):
        token = _current_run.set("chat_turn_1")
        try:
            assert await research._over_backstop("find_in_catalog") is False
            assert research._LOOSE["chat_turn_1"]["spent"] == {"find_in_catalog": 1}
        finally:
            _current_run.reset(token)
            research._LOOSE.clear()

