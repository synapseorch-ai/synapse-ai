"""A failed build says why it stopped, whether the run or one of its steps recorded the reason (F1483; TRI2, pro
drafts/tri2/TRIAGE.md class 1).

TRI2's prod run (11-09): a first build in a new workspace on Claude Sonnet 4.6 was refused at its planner's second
pass by the model gateway, "The model wallet is empty. Add balance under Settings, Billing to continue.", kept on the
step's history entry with its `model_failure`. The run's own reason (`__run__`) was empty, and the build's line read
only that, so the panel said "This didn't build on Claude Sonnet 4.6: it stopped before anything was saved." — after
it had taken away the error that said the real reason (`builderEvents.ts`, `build_failed`).

The line now reads what every other reader of a stopped run reads (`run_error.failure_reason`): the run's own reason,
else its newest failed step's. A model's failure is said as `model_failures` says one: the gateway's own sentence, or
ours for its kind, never the provider's words (D118).
"""
from __future__ import annotations

import pytest

from core import builder_tools  # noqa: F401 — loads the builder's modules first, as at boot
from core import try_stronger
from core.model_failures import MODEL_FAILURE
from core.native_builder import effort
from core.native_builder import models_menu as MM

SESSION = "builder::panel:f1483"
WALLET = "The model wallet is empty. Add balance under Settings, Billing to continue."
#: As the engine keeps it (`run_error.reason_of`): the class name, then the gateway's refusal, as on prod.
WALLET_REASON = f"LLMError: Model gateway refused the call: {WALLET}"


def _entry(id):
    return MM.ModelEntry(id=id, name=id.split(".")[1], provider="claude", source="managed", tools="native",
                         vision=False, reasoning=True, price_in=3.0, price_out=15.0, can_pay=True)


@pytest.fixture(autouse=True)
def menu(monkeypatch):
    async def fake():
        return [_entry("synapse.claude-haiku"), _entry("synapse.claude-sonnet")]

    monkeypatch.setattr(MM, "menu", fake)


def _step(step_id, status, **more):
    return {"step_id": step_id, "step_name": step_id, "step_type": "agent", "status": status, **more}


async def _failed_build(run_id, history, *, run_reason=None):
    from core.models_orchestration import OrchestrationRun
    from core.orchestration.engine import OrchestrationEngine
    from core.orchestration.run_error import set_run_error
    from core.orchestration.state import SharedState

    run = OrchestrationRun(run_id=run_id, orchestration_id="orch_native_builder", session_id=SESSION, status="failed",
                           current_step_id="step_plan", step_history=history,
                           shared_state={"intent": "create",
                                         OrchestrationEngine.MODEL_OVERRIDE_KEY: "synapse.claude-sonnet"})
    if run_reason:
        set_run_error(run, run_reason)
    await SharedState(run).checkpoint()


async def test_the_gateway_s_refusal_on_a_step_is_said_in_its_own_words(client):
    """TRI2's run, as prod kept it: the planner's first pass done, its second refused by the gateway."""
    await _failed_build("f1483_wallet", [
        _step("step_classify", "completed"),
        _step("step_plan", "completed"),
        _step("step_plan", "failed", error=WALLET_REASON, model_failure={"code": "quota_exhausted", "ours": False}),
    ])
    said = await effort.failure_notice("f1483_wallet")
    assert said == {"type": "builder_notice", "reason": "build_failed", "final": False,
                    "message": f"This didn't build on claude-sonnet: {WALLET}"}


async def test_a_provider_s_refusal_is_said_in_our_words_never_its_own(client):
    # A key's shape, built when the test runs: written whole it would be a key to a secret scanner, and the cloud's
    # vendored copy of this file is scanned (pro's pre-commit).
    key = "sk-ant-" + "api03-" + "A" * 24
    raw = f"LLMError: anthropic 401: invalid x-api-key {key} for org org_123"
    await _failed_build("f1483_provider", [
        _step("step_plan", "failed", error=raw, model_failure={"code": "provider_refused", "ours": False}),
    ])
    message = (await effort.failure_notice("f1483_provider"))["message"]
    assert message == f"This didn't build on claude-sonnet: {MODEL_FAILURE['provider_refused']}"
    assert "x-api-key" not in message and "org_123" not in message and "sk-ant" not in message
    assert key not in message and "A" * 24 not in message


async def test_a_step_s_other_failure_is_said_as_the_run_records_it(client):
    await _failed_build("f1483_timeout", [
        _step("step_plan", "failed", error="Step 'Plan It' timed out after 1200 seconds."),
    ])
    message = (await effort.failure_notice("f1483_timeout"))["message"]
    assert message == "This didn't build on claude-sonnet: Step 'Plan It' timed out after 1200 seconds."


async def test_the_run_s_own_reason_is_still_said_first(client):
    await _failed_build("f1483_run_reason", [
        _step("step_plan", "failed", error=WALLET_REASON, model_failure={"code": "quota_exhausted", "ours": False}),
    ], run_reason="The run reached its time budget")
    message = (await effort.failure_notice("f1483_run_reason"))["message"]
    assert message == "This didn't build on claude-sonnet: The run reached its time budget."


async def test_a_build_with_no_reason_anywhere_keeps_the_plain_line(client):
    await _failed_build("f1483_none", [_step("step_plan", "failed")])
    message = (await effort.failure_notice("f1483_none"))["message"]
    assert message == "This didn't build on claude-sonnet: it stopped before anything was saved."


async def test_both_hosts_end_a_failed_build_with_the_step_s_reason(client):
    """`end_events` is what the self-hosted route and cloud's fleet adapter both send before the stream ends."""
    await _failed_build("f1483_end", [
        _step("step_plan", "failed", error=WALLET_REASON, model_failure={"code": "quota_exhausted", "ours": False}),
    ])
    events = await try_stronger.end_events("f1483_end", retried=True)
    assert [e["type"] for e in events] == ["builder_notice"]
    assert events[0]["message"] == f"This didn't build on claude-sonnet: {WALLET}"
