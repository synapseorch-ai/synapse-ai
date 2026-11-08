"""
A response cap is a setting someone can set (F263).

"Gemini returned only reasoning — raise the response cap or lower the reasoning
effort", with the chat page's "Settings → Models" beside it, sent people to a
control that did not exist. `max_output_tokens` was read by the dispatcher and
refused by the settings route (422, "Extra inputs are not permitted"); no screen
offered it; an agent could not have one of its own (the model dropped the key);
and OpenAI, the OpenAI-compatible providers, the model gateway and Ollama never
received a cap at all. For Gemini 3 the cap was not sized to its thinking level
either: a long 3.1 Pro turn at medium thought through all 12,288 tokens and
answered nothing (stress pass S2, 2026-09-27).

Left unset, every provider behaves as it did (Naveen, 2026-09-28): a default of
8,192 sent to a local model with a 4k context would start failing, and an OpenAI
answer longer than 8k that works today would be cut.
"""
import pytest

import core.llm_providers as llm
from core import reasoning as R
from _fakes import seed as S
from _fakes.fake_http import FakeResponse, install_httpx


def _openai_ok(text="ok"):
    return FakeResponse({"usage": {"prompt_tokens": 1, "completion_tokens": 1},
                         "choices": [{"message": {"content": text}}]})


def _capture_http(monkeypatch):
    """Every POST body the adapters send, in order."""
    bodies = []

    def handler(url, kwargs):
        bodies.append(kwargs.get("json") or {})
        if url.endswith("/api/chat"):
            return FakeResponse({"message": {"content": "ok"}, "done_reason": "stop"})
        return _openai_ok()

    install_httpx(monkeypatch, handler)
    return bodies


@pytest.fixture
def no_usage(monkeypatch):
    from core import usage_tracker

    async def nothing(**_kwargs):
        return None

    monkeypatch.setattr(usage_tracker, "log_usage", nothing)


# ── the workspace setting ────────────────────────────────────────────────────

class TestTheWorkspaceCap:
    async def test_the_settings_route_takes_it(self, client):
        resp = await client.post("/api/settings", json={"max_output_tokens": 16384})
        assert resp.status_code == 200, resp.text
        assert (await client.get("/api/settings")).json()["max_output_tokens"] == 16384

    async def test_blank_means_automatic_again(self, client):
        await client.post("/api/settings", json={"max_output_tokens": 16384})
        resp = await client.post("/api/settings", json={"max_output_tokens": None})
        assert resp.status_code == 200, resp.text
        assert (await client.get("/api/settings")).json().get("max_output_tokens") is None

    @pytest.mark.parametrize("bad", [0, 12, -5, 10_000_000])
    async def test_a_cap_no_model_could_use_is_refused(self, client, bad):
        resp = await client.post("/api/settings", json={"max_output_tokens": bad})
        assert resp.status_code == 422


# ── an agent's own ───────────────────────────────────────────────────────────

class TestAnAgentsCap:
    async def test_the_agents_route_keeps_it(self, client):
        body = {**S.make_agent(id="agent_capped"), "max_output_tokens": 2048}
        resp = await client.post("/api/agents", json=body)
        assert resp.status_code == 200, resp.text
        assert resp.json()["max_output_tokens"] == 2048
        listed = {a["id"]: a for a in (await client.get("/api/agents")).json()}
        assert listed["agent_capped"]["max_output_tokens"] == 2048

    async def test_the_agents_cap_reaches_the_model_call(self, fake_llm):
        await _run(fake_llm, S.make_agent(tools=[], skip_default_tools=True, max_output_tokens=2048))
        assert fake_llm.calls[0].get("max_output_tokens") == 2048

    async def test_an_agent_with_none_leaves_it_to_the_workspace(self, fake_llm):
        await _run(fake_llm, S.make_agent(tools=[], skip_default_tools=True))
        assert fake_llm.calls[0].get("max_output_tokens") is None


async def _run(fake_llm, agent):
    import types

    from core.react_engine import run_agent_step

    fake_llm.script(["done"])
    server = types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})
    return [ev async for ev in run_agent_step(
        message="hi", agent_id=agent["id"], session_id="s_cap", server_module=server,
        agent_override=agent, max_turns=2,
    )]


# ── every provider reads an explicit cap ─────────────────────────────────────

class TestEveryProviderReadsAnExplicitCap:
    """Through `generate_response_ex`, to the body each adapter actually sends."""

    SETTINGS = {
        "openai_key": "k",
        "openai_compatible_base_url": "http://compat.test", "openai_compatible_key": "k",
        "local_compatible_base_url": "http://local.test",
        "model_gateway_base_url": "http://gateway.test", "model_gateway_key": "k",
    }

    @pytest.mark.parametrize("model,field", [
        ("gpt-4o", "max_completion_tokens"),
        ("oaic.some-model", "max_tokens"),
        ("locv1.some-model", "max_tokens"),
        ("synapse.some-model", "max_tokens"),
    ])
    async def test_the_workspace_cap_is_sent(self, monkeypatch, no_usage, model, field):
        bodies = _capture_http(monkeypatch)
        await llm.generate_response_ex("q", "s", "cloud", model, {**self.SETTINGS, "max_output_tokens": 3000})
        assert bodies[-1].get(field) == 3000, bodies[-1]

    async def test_ollama_gets_it_as_num_predict(self, monkeypatch, no_usage):
        bodies = _capture_http(monkeypatch)
        tools = [{"type": "function", "function": {"name": "t", "parameters": {"type": "object"}}}]
        await llm.generate_response_ex("q", "s", "local", "ollama.llama3", {"max_output_tokens": 3000},
                                       tools=tools)
        assert bodies[-1].get("options", {}).get("num_predict") == 3000, bodies[-1]

    async def test_a_call_with_its_own_cap_beats_the_workspace(self, monkeypatch, no_usage):
        bodies = _capture_http(monkeypatch)
        await llm.generate_response_ex("q", "s", "cloud", "oaic.some-model",
                                       {**self.SETTINGS, "max_output_tokens": 3000},
                                       max_output_tokens=1500)
        assert bodies[-1].get("max_tokens") == 1500

    async def test_an_agents_cap_reaches_a_provider_that_had_a_default(self, monkeypatch, no_usage):
        bodies = _capture_http(monkeypatch)
        await llm.generate_response_ex("q", "s", "cloud", "grok-2", {"grok_key": "k"},
                                       max_output_tokens=1500)
        assert bodies[-1].get("max_tokens") == 1500


class TestUnsetKeepsTodaysBehaviour:
    @pytest.mark.parametrize("model,field", [
        ("gpt-4o", "max_completion_tokens"),
        ("oaic.some-model", "max_tokens"),
        ("locv1.some-model", "max_tokens"),
        ("synapse.some-model", "max_tokens"),
    ])
    async def test_no_cap_where_there_was_none(self, monkeypatch, no_usage, model, field):
        bodies = _capture_http(monkeypatch)
        await llm.generate_response_ex("q", "s", "cloud", model, TestEveryProviderReadsAnExplicitCap.SETTINGS)
        assert field not in bodies[-1] and "max_tokens" not in bodies[-1], bodies[-1]

    async def test_ollama_is_sent_no_cap(self, monkeypatch, no_usage):
        bodies = _capture_http(monkeypatch)
        tools = [{"type": "function", "function": {"name": "t", "parameters": {"type": "object"}}}]
        await llm.generate_response_ex("q", "s", "local", "ollama.llama3", {}, tools=tools)
        assert "num_predict" not in (bodies[-1].get("options") or {})

    async def test_the_default_stays_where_it_was(self, monkeypatch, no_usage):
        bodies = _capture_http(monkeypatch)
        await llm.generate_response_ex("q", "s", "cloud", "grok-2", {"grok_key": "k"})
        assert bodies[-1].get("max_tokens") == llm.DEFAULT_MAX_OUTPUT_TOKENS


# ── Gemini 3: a cap sized to how hard it thinks ──────────────────────────────

class TestGemini3IsSizedToItsThinkingLevel:
    """Gemini 3 takes a level, not a budget, and its thinking at that level is
    not bounded by anything we send — only the cap is. At medium it got
    8,192 + 4,096 and a long turn spent every token of it thinking."""

    @pytest.mark.parametrize("model", ["gemini-3-flash-preview", "gemini-3.1-pro-preview"])
    def test_medium_leaves_room_past_what_ran_out(self, model):
        plan = R.plan_for(model, "medium", 8192, None)
        assert plan.max_output_tokens > 12_288

    @pytest.mark.parametrize("model", ["gemini-3-flash-preview", "gemini-3.1-pro-preview"])
    def test_it_grows_with_the_level_and_stays_under_the_models_ceiling(self, model):
        caps = [R.plan_for(model, e, 8192, None).max_output_tokens for e in ("low", "medium", "high")]
        assert caps == sorted(caps) and len(set(caps)) == 3
        assert caps[-1] <= 65_536

    def test_a_larger_cap_someone_set_is_kept(self):
        assert R.plan_for("gemini-3-flash-preview", "low", 60_000, None).max_output_tokens == 60_000

    def test_gemini_25_is_unchanged(self):
        """It takes a budget, which the cap already clears (`_GEMINI_BUDGET`)."""
        assert R.plan_for("gemini-2.5-flash", "medium", 8192, None).max_output_tokens == 8192 + 4096


# ── the error names a control that exists ────────────────────────────────────

class TestTheErrorNamesARealControl:
    def test_gemini(self):
        import types

        part = types.SimpleNamespace(function_call=None, text="thinking…", thought=True)
        cand = types.SimpleNamespace(finish_reason=types.SimpleNamespace(name="MAX_TOKENS"),
                                     content=types.SimpleNamespace(parts=[part]))
        with pytest.raises(llm.EmptyAnswer) as caught:
            llm._extract_gemini_response(types.SimpleNamespace(candidates=[cand]))
        assert "Max response length" in str(caught.value)
        assert "response cap" not in str(caught.value)

    def test_anthropic(self):
        import types

        block = types.SimpleNamespace(type="thinking", thinking="thinking…")
        with pytest.raises(llm.EmptyAnswer) as caught:
            llm._extract_anthropic_response(types.SimpleNamespace(content=[block]))
        assert "Max response length" in str(caught.value)
        assert "response cap" not in str(caught.value)
