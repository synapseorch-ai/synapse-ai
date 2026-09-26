"""
A failed run's own words reach a member in the app, and nobody else (F231).

An orchestrator agent turns its run's final line into its answer, and for a
failed run that line is `_build_final_response`'s "Orchestration failed.
Error: <the step's real error>". F133 settled who may read an agent's failure
— a member in the app, not a widget visitor, not someone on a channel, not a
v1 caller — and this is the same failure by another road: the words passed the
widget's allowlist as `final`, were saved as the turn's answer, went to the
channel as "⚠️ Orchestration failed. Error: …" and to v1 as the response.
"""
import json

import pytest

from _fakes import seed as S
from tests.unit.test_human_step_channel import messaging_on, rest, wired  # noqa: F401 — fixtures

WORDS = "vault_password_for_payroll_db"


@pytest.fixture
def failing_step(monkeypatch):
    """The orchestration's one step raises with words that must stay inside."""
    from core.orchestration import steps as steps_mod

    async def boom(self, step, run, engine):
        raise KeyError(WORDS)
        yield  # pragma: no cover — makes this an async generator

    monkeypatch.setattr(steps_mod.PrintStepExecutor, "execute", boom)


async def _orchestrator(seed_orchestration, agent_id="agent_f231"):
    orch = await seed_orchestration()  # a single print step, made to fail
    agent = S.make_orchestrator_agent(orch["id"], id=agent_id)
    await S.seed_agents([agent])
    return agent


class TestWhoSeesAFailedRunsWords:
    async def test_a_member_on_the_chat_page_does(self, client, seed_orchestration, failing_step):
        agent = await _orchestrator(seed_orchestration)
        stream = await client.post("/chat/stream", json={"message": "go", "agent_id": agent["id"]})
        assert WORDS in stream.text, "the member's page says what failed (F195)"
        frames = [json.loads(line[6:]) for line in stream.text.split("\n") if line.startswith("data: {")]
        errors = [f for f in frames if f.get("type") == "error"]
        assert errors and WORDS in errors[-1]["message"], "as the turn's error, the failed-turn bar"
        assert not [f for f in frames if f.get("type") == "response"], "not as the agent's answer"

    async def test_not_a_widget_visitor(self, client, seed_orchestration, failing_step):
        from core.chat_widgets import limits
        limits.reset()
        agent = await _orchestrator(seed_orchestration, "agent_f231_widget")
        widget = (await client.post("/api/widgets", json={
            "name": "Support", "agent_id": agent["id"], "allowed_origins": ["https://acme.com"]})).json()
        token = (await client.post("/api/widget/session", json={
            "public_key": widget["public_key"], "origin": "https://acme.com"})).json()["token"]
        headers = {"Authorization": f"Bearer {token}"}

        turn = await client.post("/api/widget/chat", json={"message": "go"}, headers=headers)
        assert WORDS not in turn.text
        assert '"type": "error"' in turn.text, "the widget shows its own generic line"
        history = await client.get("/api/widget/history", headers=headers)
        assert WORDS not in history.text
        from core.chat_turns import FAILED_RUN_ANSWER
        assert history.json()["messages"][-2:] == [
            {**history.json()["messages"][-2], "role": "user", "content": "go"},
            {**history.json()["messages"][-1], "role": "assistant", "content": FAILED_RUN_ANSWER},
        ], "the conversation keeps the question, answered by a marker"
        limits.reset()

    async def test_not_a_v1_caller(self, client, api_key, seed_orchestration, failing_step):
        agent = await _orchestrator(seed_orchestration, "agent_f231_v1")
        sync = await client.post("/api/v1/chat", json={"message": "go", "agent": agent["id"]},
                                 headers=api_key["headers"])
        assert WORDS not in sync.text
        assert sync.status_code == 500, "a failed run is not a `completed` answer"
        stream = await client.post("/api/v1/chat/stream", json={"message": "go", "agent": agent["id"]},
                                   headers=api_key["headers"])
        assert WORDS not in stream.text

    async def test_a_v1_caller_is_told_which_run_failed(
        self, client, api_key, seed_orchestration, failing_step, caplog,
    ):
        """B2's wording (D134): the failure is named, with the run to read back.

        An API-key holder may read the run's step errors at the v1 read-back
        (F117), so the id is the useful part; the words themselves stay out.
        It used to be "An internal error occurred. Check server logs for
        details." and a full traceback in the log.
        """
        from core.orchestration.run_view import list_runs

        agent = await _orchestrator(seed_orchestration, "agent_f231_v1_words")
        with caplog.at_level("ERROR", logger="api_v1"):
            sync = await client.post("/api/v1/chat", json={"message": "go", "agent": agent["id"]},
                                     headers=api_key["headers"])
        run_id = (await list_runs(limit=1))[0]["run_id"]
        said = (f"The agent's workflow failed before answering. "
                f"Read run {run_id} at /api/v1/orchestrations/runs/{run_id}.")
        assert sync.status_code == 500
        assert sync.json()["detail"] == said
        assert not [r for r in caplog.records if r.exc_info], "one log line, not a traceback"

        stream = await client.post("/api/v1/chat/stream", json={"message": "go", "agent": agent["id"]},
                                   headers=api_key["headers"])
        frames = [json.loads(line[6:]) for line in stream.text.split("\n") if line.startswith("data: {")]
        (error,) = [f for f in frames if f.get("type") == "error"]
        assert error["run_id"] == (await list_runs(limit=1))[0]["run_id"]
        assert error["message"].startswith("The agent's workflow failed before answering.")

    async def test_not_someone_on_a_channel(self, wired, seed_orchestration, failing_step):
        from core.messaging import store as channel_store
        agent = await _orchestrator(seed_orchestration, "agent_f231_chan")
        await channel_store.set_assignments(wired.channel["id"], [agent["id"]])
        await wired.adapter._dispatch("c1", "go", "u1")

        (reply,) = wired.adapter.sent
        assert WORDS not in reply
        from core.messaging.manager import FAILED_REPLY
        assert reply == FAILED_REPLY
