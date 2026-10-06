"""
A first message no model can answer says so, and says where to fix it (F182).

Measured on a fresh install (OSS test pass, 2026-09-25): the default model is
`ollama.mistral`, nothing was listening, and the page greeted the person with
"SYNAPSE · READY" — then answered their first message with httpx's own text,
"Local Agent Error: Server error '500 Internal Server Error' for url
'http://127.0.0.1:18902/api/chat' For more information check:
https://developer.mozilla.org/…", cut off by the banner. Nothing pointed to
Settings → Models, and nothing on the page had checked anything before saying
Ready.

Three things are held here: the chat page's error frame says when it was the
model that failed, so the page can point at the models screen; Ollama's failure
is Ollama's own words (or that it is not reachable) rather than an HTTP
library's; and `/api/status` says whether the model the page is about to use
can answer at all — without a paid call, and without guessing about providers
that can authenticate outside settings.
"""
import json

import httpx
import pytest

import core.llm_providers as _llm_mod
from _fakes.fake_http import FakeResponse, install_httpx
from core.llm_providers import LLMError

# Captured before the autouse fake swaps the module attribute.
_REAL_GENERATE = _llm_mod.generate_response


def _frames(text: str) -> list[dict]:
    return [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ")]


class TestTheChatPageIsToldItWasTheModel:
    async def test_a_model_failure_is_marked_as_one(self, client, seed_agent, monkeypatch):
        import core.react_engine as re_mod

        async def unreachable(*_a, **_kw):
            raise LLMError("Local Agent Error: Ollama is not reachable at http://127.0.0.1:9.")

        monkeypatch.setattr(re_mod, "llm_generate_response", unreachable)
        agent = await seed_agent()
        stream = await client.post("/chat/stream", json={"message": "hi", "agent_id": agent["id"]})

        error = next(f for f in _frames(stream.text) if f["type"] == "error")
        assert error.get("kind") == "model", error
        assert "Ollama is not reachable" in error["message"], "the provider's words still reach the page"

    async def test_an_error_that_is_not_the_model_is_not_marked(self, client):
        # No workspace agent: F186's sentence, which is about agents, not models.
        stream = await client.post("/chat/stream", json={"message": "hi"})

        error = next(f for f in _frames(stream.text) if f["type"] == "error")
        assert "No agent to answer yet" in error["message"]
        assert error.get("kind") != "model", error


class TestOllamaSaysWhatHappened:
    async def test_nothing_listening(self, monkeypatch):
        def refuse(url, kwargs):
            raise httpx.ConnectError("All connection attempts failed")

        install_httpx(monkeypatch, refuse)
        with pytest.raises(LLMError) as err:
            await _REAL_GENERATE("hi", "sys", "local", "ollama.mistral",
                                 {"ollama_base_url": "http://127.0.0.1:9"})
        assert str(err.value) == "Local Agent Error: Ollama is not reachable at http://127.0.0.1:9."

    async def test_a_model_not_pulled_is_ollamas_own_sentence(self, monkeypatch):
        said = "model 'mistral' not found, try pulling it first"
        install_httpx(monkeypatch, FakeResponse({"error": said}, status_code=404,
                                                text=json.dumps({"error": said})))
        with pytest.raises(LLMError) as err:
            await _REAL_GENERATE("hi", "sys", "local", "ollama.mistral", {})
        message = str(err.value)
        assert message.startswith("Local Agent Error")
        assert said in message
        assert "developer.mozilla.org" not in message

    async def test_a_refusal_with_no_body_says_the_status(self, monkeypatch):
        install_httpx(monkeypatch, FakeResponse(status_code=500, text=""))
        with pytest.raises(LLMError) as err:
            await _REAL_GENERATE("hi", "sys", "local", "ollama.mistral",
                                 {"ollama_base_url": "http://127.0.0.1:9"})
        message = str(err.value)
        assert message == "Local Agent Error: Ollama at http://127.0.0.1:9 answered 500."

    async def test_the_tool_calling_path_says_the_same(self, monkeypatch):
        said = "model 'mistral' not found, try pulling it first"
        install_httpx(monkeypatch, FakeResponse({"error": said}, status_code=404,
                                                text=json.dumps({"error": said})))
        with pytest.raises(LLMError) as err:
            await _REAL_GENERATE("hi", "sys", "local", "ollama.mistral", {},
                                 tools=[{"type": "function", "function": {"name": "get_time"}}])
        assert said in str(err.value) and "developer.mozilla.org" not in str(err.value)


@pytest.fixture
def ollama(monkeypatch):
    """What Ollama's model list answers, without a network."""
    from core import model_readiness

    box = {"answer": ["mistral:latest"], "asked": []}

    async def models(base: str) -> list[str]:
        box["asked"].append(base)
        answer = box["answer"]
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(model_readiness, "_ollama_models", models)
    return box


async def _status(client, **settings) -> dict:
    if settings:
        saved = await client.post("/api/settings", json=settings)
        assert saved.status_code == 200, saved.text
    return (await client.get("/api/status")).json()


class TestStatusSaysWhetherTheModelCanAnswer:
    async def test_ollama_not_reachable(self, client, ollama):
        ollama["answer"] = httpx.ConnectError("refused")
        status = await _status(client, model="ollama.mistral", ollama_base_url="http://127.0.0.1:9")
        assert status["model_ready"] is False
        assert status["model_problem"] == "Ollama is not reachable at http://127.0.0.1:9."

    async def test_ollama_up_without_the_model(self, client, ollama):
        ollama["answer"] = ["llama3:latest"]
        status = await _status(client, model="ollama.mistral")
        assert status["model_ready"] is False
        assert "mistral" in status["model_problem"] and "ollama pull mistral" in status["model_problem"]

    async def test_ollama_up_with_the_model(self, client, ollama):
        status = await _status(client, model="ollama.mistral")
        assert status["model_ready"] is True
        assert status["model_problem"] is None

    async def test_a_tagged_model_needs_that_tag(self, client, ollama):
        ollama["answer"] = ["mistral:7b"]
        assert (await _status(client, model="ollama.mistral"))["model_ready"] is False
        assert (await _status(client, model="ollama.mistral:7b"))["model_ready"] is True

    async def test_ollama_refusing_the_list(self, client, ollama):
        request = httpx.Request("GET", "http://127.0.0.1:9/api/tags")
        ollama["answer"] = httpx.HTTPStatusError(
            "boom", request=request, response=httpx.Response(503, request=request))
        status = await _status(client, model="ollama.mistral", ollama_base_url="http://127.0.0.1:9")
        assert status["model_ready"] is False
        assert status["model_problem"] == "Ollama at http://127.0.0.1:9 answered 503."

    async def test_a_key_provider_with_no_key(self, client, ollama):
        status = await _status(client, model="gemini-2.5-flash")
        assert status["model_ready"] is False
        assert status["model_problem"] == "No Gemini key is saved."
        assert not ollama["asked"], "a cloud model never asks Ollama"

    async def test_a_key_provider_with_a_key(self, client, ollama):
        status = await _status(client, model="gemini-2.5-flash", gemini_key="dummy-key-not-real-1234")
        assert status["model_ready"] is True
        assert status["model_problem"] is None
        assert "dummy-key-not-real-1234" not in json.dumps(status)

    async def test_a_compatible_server_with_no_address(self, client, ollama):
        status = await _status(client, model="oaic.some-model")
        assert status["model_ready"] is False
        assert status["model_problem"] == "No OpenAI-compatible server address is saved."

    async def test_bedrock_is_not_judged(self, client, ollama):
        # boto's default chain (a profile, an instance role) can hold keys that
        # settings never sees, so "not ready" would be a guess.
        status = await _status(client, model="bedrock.anthropic.claude-3-5-sonnet-20240620-v1:0")
        assert status["model_ready"] is True

    async def test_the_active_agents_own_model_is_the_one_checked(self, client, seed_agent, ollama, monkeypatch):
        import core.routes.agents as agents_mod
        monkeypatch.setattr(agents_mod, "active_agent_id", None)  # module memory; restored after
        agent = await seed_agent(model="gemini-2.5-flash")
        await client.post("/api/agents/active", json={"agent_id": agent["id"]})
        status = await _status(client, model="ollama.mistral")
        assert status["model"] == "gemini-2.5-flash"
        assert status["model_problem"] == "No Gemini key is saved."


class TestTheOllamaListIsReadOnce:
    async def test_names_come_from_the_tags_route(self, monkeypatch):
        from core import model_readiness

        seen = {}

        def handler(url, kwargs):
            seen["url"] = url
            seen["timeout"] = kwargs.get("timeout")
            return FakeResponse({"models": [{"name": "mistral:latest"}, {"name": "llama3:8b"}]})

        install_httpx(monkeypatch, handler)
        assert await model_readiness._ollama_models("http://127.0.0.1:9") == ["mistral:latest", "llama3:8b"]
        assert seen["url"] == "http://127.0.0.1:9/api/tags"
        assert seen["timeout"] and seen["timeout"] <= 3, "a page load never waits long on it"

    async def test_many_models_share_one_list(self, ollama):
        """A workflow's steps name several models; Ollama's list is read once for them (F313)."""
        from core.model_readiness import model_problems

        said = await model_problems({}, ["ollama.mistral", "ollama.llama3", "ollama.qwen", "gpt-5"])
        assert len(ollama["asked"]) == 1
        assert said["ollama.mistral"] is None and said["gpt-5"] == "No OpenAI key is saved."
        assert "ollama pull llama3" in said["ollama.llama3"] and "ollama pull qwen" in said["ollama.qwen"]


async def _workflow(seed_steps, *, agents=(), name="Photo desk"):
    """A workflow agent over `seed_steps`, made the active agent; seeds replace, so all at once."""
    from _fakes import seed as S

    orch = S.make_orchestration(id="orch_f313s", entry_step_id=seed_steps[0]["id"], steps=seed_steps)
    await S.seed_orchestrations([orch])
    top = S.make_orchestrator_agent("orch_f313s", id="agent_f313s", name=name)
    await S.seed_agents([*agents, top])
    return top


def _agent_step(sid, *, model=None, takes=False, nxt=None, agent_id="agent_f313_eyes"):
    step = {"id": sid, "name": sid.title(), "type": "agent", "agent_id": agent_id, "prompt_template": "go",
            "output_key": f"{sid}_out", "next_step_id": nxt}
    if model:
        step["model"] = model
    if takes:
        step["pass_images"] = True
    return step


def _eyes(model="gpt-5"):
    from _fakes import seed as S

    return S.make_agent(id="agent_f313_eyes", name="Eyes", model=model)


class TestAWorkflowAgentIsJudgedByItsSteps:
    """Its readiness and Attach come from its steps' models, not the workspace default (F313)."""

    @pytest.fixture(autouse=True)
    def _no_active(self, monkeypatch):
        import core.routes.agents as agents_mod
        monkeypatch.setattr(agents_mod, "active_agent_id", None)  # module memory; restored after

    async def _activate(self, client, agent):
        await client.post("/api/agents/active", json={"agent_id": agent["id"]})

    async def test_attach_is_refused_when_no_step_takes_attachments(self, client, ollama):
        top = await _workflow([_agent_step("look")], agents=[_eyes()])
        flags = (await _status(client, model="ollama.mistral"))["agents"][top["id"]]
        assert flags["can_see_images"] is False
        assert flags["attach_problem"].startswith("No step of this workflow takes attachments")

    async def test_attach_is_offered_when_a_step_takes_them_on_a_model_that_sees(self, client, ollama):
        top = await _workflow([_agent_step("look", takes=True)], agents=[_eyes("gpt-5")])
        flags = (await _status(client, model="deepseek-chat"))["agents"][top["id"]]
        assert flags["can_see_images"] is True and flags["attach_problem"] is None

    async def test_attach_names_the_step_whose_model_cannot_see(self, client, ollama):
        top = await _workflow([_agent_step("look", takes=True, model="deepseek-chat")], agents=[_eyes()])
        flags = (await _status(client, model="gpt-5"))["agents"][top["id"]]
        assert flags["can_see_images"] is False
        assert flags["attach_problem"] == "Step 'Look' takes attachments, but deepseek-chat cannot read images."

    async def test_a_workflow_on_ready_steps_is_ready_though_the_default_is_not(self, client, ollama):
        top = await _workflow([_agent_step("look", model="ollama.mistral")], agents=[_eyes()])
        await self._activate(client, top)
        status = await _status(client, model="gpt-5")
        assert status["model_ready"] is True and status["model_problem"] is None

    async def test_an_unready_step_is_named(self, client, ollama):
        top = await _workflow([_agent_step("research", model="gemini-2.5-flash", nxt="write"),
                               _agent_step("write", model="ollama.mistral")], agents=[_eyes()])
        await self._activate(client, top)
        status = await _status(client, model="ollama.mistral")
        assert status["model_ready"] is False
        assert status["model_problem"] == "Step 'Research' (gemini-2.5-flash): No Gemini key is saved."

    async def test_ollamas_list_is_read_once_for_every_step(self, client, ollama):
        top = await _workflow([_agent_step("a", model="ollama.mistral", nxt="b"),
                               _agent_step("b", model="ollama.llama3", nxt="c"),
                               _agent_step("c", model="ollama.qwen")], agents=[_eyes()])
        await self._activate(client, top)
        await _status(client, model="ollama.mistral")
        ollama["asked"].clear()
        status = (await client.get("/api/status")).json()
        assert len(ollama["asked"]) == 1
        assert "Step 'B' (ollama.llama3)" in status["model_problem"]
        assert "Step 'C' (ollama.qwen)" in status["model_problem"]
