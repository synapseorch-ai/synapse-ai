"""A failure of a model the deployment hosts is marked as the deployment's own, so a host need not charge for it
(F1012; Naveen 2026-10-24).

HOT1's F878: cloud's hosted Nova failed mid-stream on a tool name it could not write, and each failed step still
cost the customer a run credit — the engine recorded a failed step like any other, and cloud prices every entry.
Naveen: a failure of a model WE host (`synapse.*`, through the deployment's model gateway) — a server, stream,
timeout or throttling error — is ours, and not charged; a failure on the customer's own key, or a refusal caused by
what they sent (wallet empty, input too long, a content refusal), stays theirs.

The engine says which: `LLMError.via_gateway` (the call went through the deployment's model gateway) and
`failure_is_ours(err)`; a failed step's history entry carries `model_failure: {"code", "ours"}`, and a chat host
reads the same from the exception it is handed.
"""
import pytest

import core.llm_providers as llm
from core.model_failures import MODEL_FAILURE
from _fakes import seed as S


def _err(status=None, message="", gateway=True, code=None):
    e = llm.LLMError(message or f"failed ({status})", status=status, code=code)
    e.via_gateway = gateway
    return e


class TestWhoseFailure:
    @pytest.mark.parametrize("err", [
        _err(503), _err(500), _err(429), _err(message="The provider failed mid-answer: modelStreamErrorException"),
        _err(code=llm.PROVIDER_UNAVAILABLE),
    ])
    def test_our_models_server_stream_or_throttle_failure_is_ours(self, err):
        assert llm.failure_is_ours(err) is True

    @pytest.mark.parametrize("err", [
        _err(402, "The model wallet is empty."), _err(400, "Input is too long for the model."),
        _err(403, "Content refused."), _err(code=llm.MODEL_NO_ANSWER),
    ])
    def test_what_the_customer_sent_or_owes_is_theirs(self, err):
        assert llm.failure_is_ours(err) is False

    def test_a_failure_on_the_customers_own_key_is_theirs(self):
        assert llm.failure_is_ours(_err(503, gateway=False)) is False

    def test_anything_that_is_not_a_model_failure_is_not_ours(self):
        assert llm.failure_is_ours(RuntimeError("boom")) is False


class TestTheCallSaysWhoServedIt:
    async def _fail(self, monkeypatch, model):
        from core.providers import registry as providers

        async def broken(call, spec):
            raise llm.LLMError("Model gateway error (503): upstream unavailable", status=503)

        monkeypatch.setattr(providers, "resolve", lambda _name: broken)
        settings = {"model_gateway_base_url": "http://gateway.test/v1", "model_gateway_key": "k",
                    "openai_key": "sk-test"}
        with pytest.raises(llm.LLMError) as raised:
            # `_ex`: the suite's fake model stands in for `generate_response`.
            await llm.generate_response_ex("hi", "sys", "cloud", model, settings)
        return raised.value

    async def test_a_gateway_model_is_ours_to_serve(self, monkeypatch):
        err = await self._fail(monkeypatch, "synapse.nova-pro")
        assert err.via_gateway is True and llm.failure_is_ours(err) is True

    async def test_an_own_key_model_is_not(self, monkeypatch):
        err = await self._fail(monkeypatch, "gpt-4.1-mini")
        assert err.via_gateway is False and llm.failure_is_ours(err) is False


class TestTheFailedStepSaysSo:
    async def _run(self, monkeypatch, seed_agent, run_id, *, gateway):
        import types

        import core.react_engine as re_mod
        from core.models_orchestration import Orchestration
        from core.orchestration.engine import OrchestrationEngine
        from core.orchestration.state import get_state_backend

        async def model(*_a, **_kw):
            raise _err(503, "Model gateway error (503): upstream unavailable", gateway=gateway)

        monkeypatch.setattr(re_mod, "llm_generate_response", model)
        agent = await seed_agent(tools=[], skip_default_tools=True)
        orch = S.make_orchestration(id=f"orch_{run_id}", entry_step_id="a", steps=[
            {"id": "a", "name": "Answer", "type": "agent", "agent_id": agent["id"], "next_step_id": None}])
        server = types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})
        [e async for e in OrchestrationEngine(Orchestration.model_validate(orch), server).run("go", run_id=run_id)]
        return await get_state_backend().load(run_id)

    async def test_our_models_failure_is_marked_ours_on_the_entry(self, monkeypatch, seed_agent):
        run = await self._run(monkeypatch, seed_agent, "run_f1012_ours", gateway=True)
        [entry] = run.step_history
        assert entry["status"] == "failed"
        # F1502: the entry keeps what a person is told of it, ours for its kind here.
        assert entry.get("model_failure") == {"code": llm.PROVIDER_UNAVAILABLE, "ours": True,
                                               "said": MODEL_FAILURE[llm.PROVIDER_UNAVAILABLE]}

    async def test_an_own_key_failure_is_marked_theirs(self, monkeypatch, seed_agent):
        run = await self._run(monkeypatch, seed_agent, "run_f1012_theirs", gateway=False)
        [entry] = run.step_history
        # F1502: the entry keeps what a person is told of it, ours for its kind here.
        assert entry.get("model_failure") == {"code": llm.PROVIDER_UNAVAILABLE, "ours": False,
                                               "said": MODEL_FAILURE[llm.PROVIDER_UNAVAILABLE]}
