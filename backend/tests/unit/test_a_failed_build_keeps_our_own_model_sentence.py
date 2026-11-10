"""A failed build keeps a model failure's own sentence when we wrote it (F1502; BLD2 on OSS 2.0.1, eb 11-10).

A build on a Gemini CLI that was not signed in said "This didn't build on cli.gemini.flash: The model call failed."
The step's reason was ours and said what to do: "CLI provider 'cli.gemini.flash' is not signed in. Run `gemini` in a
terminal and sign in with Google, then retry." F1483 let only the gateway's sentence through (`recorded_sentence`),
and replaced every other with our words for its kind, or the plain line where its kind has none.

Now an error our code raises in its own words carries that sentence (`LLMError.said`), without what a provider or a
CLI printed (its "Details"); the failed step keeps it with its failure (`model_failure.said`, from `failure_sentence`),
and the build's line says it. A provider's own words are still never said (D118); a run kept before keeps its reading.
"""
from __future__ import annotations

import pytest

from core import builder_tools  # noqa: F401 — loads the builder's modules first, as at boot
from core import llm_providers as L
from core.model_failures import MODEL_FAILURE, failure_sentence
from core.native_builder import effort
from core.native_builder import models_menu as MM
from core.orchestration.run_error import model_failure_of, reason_of

SESSION = "builder::panel:f1502"
CLI = "cli.gemini.flash"
SIGNED_OUT = ("Error: Please set an Auth method in your settings.json or specify GEMINI_API_KEY\n"
              "    at validateAuth (file:///usr/lib/gemini/index.js:12:9)\n"
              "    at main (file:///usr/lib/gemini/index.js:40:3)")
OURS = f"CLI provider '{CLI}' is not signed in. {L._CLI_SIGN_IN['cli.gemini']}, then retry."


@pytest.fixture(autouse=True)
def menu(monkeypatch):
    async def fake():
        return []

    monkeypatch.setattr(MM, "menu", fake)


async def _failed_build(run_id, exc):
    """A build whose plan step failed on `exc`, kept as the engine keeps it (`reason_of`, `model_failure_of`)."""
    from core.models_orchestration import OrchestrationRun
    from core.orchestration.engine import OrchestrationEngine
    from core.orchestration.state import SharedState

    entry = {"step_id": "step_plan", "step_name": "Plan It", "step_type": "agent", "status": "failed",
             "error": reason_of(exc)}
    if (failure := model_failure_of(exc)) is not None:
        entry["model_failure"] = failure
    run = OrchestrationRun(run_id=run_id, orchestration_id="orch_native_builder", session_id=SESSION, status="failed",
                           current_step_id="step_plan", step_history=[entry],
                           shared_state={"intent": "create", OrchestrationEngine.MODEL_OVERRIDE_KEY: CLI})
    await SharedState(run).checkpoint()
    return (await effort.failure_notice(run_id))["message"]


def test_a_signed_out_cli_is_said_in_our_words_without_what_it_printed():
    exc = L._cli_failure("cli.gemini", CLI, SIGNED_OUT, auth=True)
    assert failure_sentence(exc) == OURS
    assert model_failure_of(exc)["said"] == OURS


async def test_the_build_line_says_the_signed_out_cli_and_what_to_do(client):
    message = await _failed_build("f1502_signed_out", L._cli_failure("cli.gemini", CLI, SIGNED_OUT, auth=True))
    assert message == f"This didn't build on {CLI}: {OURS}"
    assert "validateAuth" not in message and "settings.json" not in message, "never what the CLI printed"


async def test_a_cli_at_its_limit_is_said_in_our_words(client):
    exc = L._cli_failure("cli.gemini", CLI, "429 RESOURCE_EXHAUSTED: Quota exceeded for quota metric", auth=False)
    message = await _failed_build("f1502_limit", exc)
    assert message == f"This didn't build on {CLI}: CLI provider '{CLI}' rate limit / capacity error."
    assert "RESOURCE_EXHAUSTED" not in message


async def test_what_a_cli_printed_alone_is_never_said(client):
    """A failure our code could not word is the CLI's own text: our words for its kind, as before."""
    exc = L._cli_failure("cli.gemini", CLI, "TypeError: cannot read properties of undefined (reading 'parts')")
    message = await _failed_build("f1502_cli_text", exc)
    assert "TypeError" not in message and "parts" not in message
    assert message == f"This didn't build on {CLI}: {MODEL_FAILURE.get(exc.code, 'The model call failed.')}"


async def test_a_providers_own_words_are_still_never_said(client):
    exc = L.LLMError("anthropic 401: invalid x-api-key for org org_123", status=401)
    message = await _failed_build("f1502_provider", exc)
    assert message == f"This didn't build on {CLI}: {MODEL_FAILURE['provider_refused']}"
    assert "x-api-key" not in message and "org_123" not in message


async def test_a_step_kept_before_f1502_keeps_its_reading(client):
    """No `said` on the entry: the gateway's sentence is still found in the reason, as F1483 read it."""
    from core.models_orchestration import OrchestrationRun
    from core.orchestration.engine import OrchestrationEngine
    from core.orchestration.state import SharedState

    wallet = "The model wallet is empty. Add balance under Settings, Billing to continue."
    run = OrchestrationRun(run_id="f1502_before", orchestration_id="orch_native_builder", session_id=SESSION,
                           status="failed", current_step_id="step_plan", step_history=[
                               {"step_id": "step_plan", "status": "failed",
                                "error": f"LLMError: Model gateway refused the call: {wallet}",
                                "model_failure": {"code": "quota_exhausted", "ours": False}}],
                           shared_state={"intent": "create", OrchestrationEngine.MODEL_OVERRIDE_KEY: CLI})
    await SharedState(run).checkpoint()
    assert (await effort.failure_notice("f1502_before"))["message"] == f"This didn't build on {CLI}: {wallet}"


async def test_an_address_in_our_sentence_is_its_host_alone(monkeypatch):
    """eb's review: Ollama's configured address can carry a login or a key, and `said` reaches the person and the run's
    public record. It names scheme://host:port only; the whole address stays in the reason, as it always has."""
    import httpx

    from _fakes.fake_http import install_httpx

    def refuse(url, kwargs):
        raise httpx.ConnectError("All connection attempts failed")

    install_httpx(monkeypatch, refuse)
    base = "http://alice:s3cret-pass@ollama.internal:11434/v1?token=tok-0123456789"
    with pytest.raises(L.LLMError) as err:
        await L.generate_response("hi", "sys", "local", "ollama.mistral", {"ollama_base_url": base})
    assert err.value.said == "Ollama is not reachable at http://ollama.internal:11434."
    said = model_failure_of(err.value)["said"]
    assert said == "Ollama is not reachable at http://ollama.internal:11434."
    for secret in ("alice", "s3cret-pass", "token", "tok-0123456789", "/v1"):
        assert secret not in said and secret not in failure_sentence(err.value)
