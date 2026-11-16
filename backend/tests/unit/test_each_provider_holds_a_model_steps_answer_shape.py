"""Each provider is sent a model step's declared answer shape in the strongest form it holds, picked from one table
(F1618, commit b: the providers).

A step's declared answer reached only OpenAI, Gemini and Ollama as a schema; every Claude, on the API, on Bedrock or
through the model gateway, and every model CLI was asked for it in words alone. Now `core.answer_shape` gives each
model one rung, from one table: `schema`, the provider's own schema mode (Claude's `output_config.format`, Bedrock
Converse's `outputConfig.textFormat`, OpenAI's strict `json_schema`, Gemini's `response_json_schema`, Ollama's `format`,
a CLI's own schema flag); `forced_tool`, one `answer` tool the call must use, with thinking off, only for an older
Claude that has no schema mode, never for the Claude models that refuse a forced tool; and `prompt`, the words alone
with validation and one repair. A refusal of the format itself (not supported, too complex, a key setup that has none)
takes the next rung down, once, and the step says which rung held the answer. A call with no declaration is sent as
before.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import core.llm_providers as llm

LEADS = {"type": "object", "properties": {
    "leads": {"type": "array", "items": {
        "type": "object", "properties": {"email": {"type": "string"}, "score": {"anyOf": [{"type": "number"},
                                                                                        {"type": "null"}]}},
        "required": ["email", "score"], "additionalProperties": False}}},
    "required": ["leads"], "additionalProperties": False}
ANSWER = {"leads": [{"email": "ana@example.com", "score": 0.9}]}

#: The Claude models whose API refuses a forced `tool_choice` (a 400): never on the forced-tool rung.
REFUSE_A_FORCED_TOOL = ("claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-1", "claude-fable-5-2",
                        "claude-mythos-5-1", "claude-mythos-5-2")


def _shape():
    from core import answer_shape

    return answer_shape


# ── one table ────────────────────────────────────────────────────────────────

class TestEachModelHasOneRung:
    def test_every_catalogued_model_has_exactly_one(self):
        from core.model_pricing import DEFAULT_MODEL_PRICING

        shape = _shape()
        for model, entry in DEFAULT_MODEL_PRICING.items():
            assert shape.rung(entry.get("provider", ""), model) in shape.RUNGS, model

    @pytest.mark.parametrize("model", REFUSE_A_FORCED_TOOL)
    def test_a_claude_that_refuses_a_forced_tool_is_never_on_that_rung(self, model):
        shape = _shape()
        assert shape.rung("anthropic", model) == shape.SCHEMA
        assert shape.below("anthropic", model, shape.SCHEMA) == shape.PROMPT

    @pytest.mark.parametrize("provider, model", [
        ("anthropic", "claude-sonnet-4-6"), ("anthropic", "claude-opus-4-6"), ("anthropic", "claude-haiku-4-5"),
        ("bedrock", "bedrock.us.anthropic.claude-sonnet-4-6"), ("bedrock", "bedrock.global.anthropic.claude-opus-4-6-v1"),
        ("bedrock", "bedrock.anthropic.claude-haiku-4-5-20251001-v1:0")])
    def test_clouds_defaults_and_their_bedrock_twins_hold_a_schema(self, provider, model):
        assert _shape().rung(provider, model) == _shape().SCHEMA

    @pytest.mark.parametrize("model", ["claude-3-5-haiku-20241022", "claude-opus-4-20250514",
                                       "claude-sonnet-4-20250514"])
    def test_an_older_claude_takes_the_forced_tool(self, model):
        assert _shape().rung("anthropic", model) == _shape().FORCED_TOOL

    @pytest.mark.parametrize("provider, model", [("openai", "gpt-5"), ("gemini", "gemini-2.5-pro"),
                                                 ("ollama", "llama3.1"), ("model_gateway", "synapse.claude-sonnet")])
    def test_a_schema_mode_already_sent_is_kept(self, provider, model):
        assert _shape().rung(provider, model) == _shape().SCHEMA

    @pytest.mark.parametrize("provider, model", [("huggingface", "meta-llama/Llama-3.1-8B-Instruct"),
                                                 ("openai_compatible", "oaic.local-model"),
                                                 ("bedrock", "bedrock.amazon.nova-pro-v1:0"),
                                                 ("grok", "grok-4")])
    def test_a_model_nobody_has_measured_is_asked_in_words(self, provider, model):
        assert _shape().rung(provider, model) == _shape().PROMPT


# ── Claude on the API ────────────────────────────────────────────────────────

def _claude_answers(monkeypatch, *responses):
    """`_anthropic_create` answering each call with the next of `responses` (an exception is raised)."""
    asked: list[dict] = []

    async def create(client, kwargs):
        asked.append(json.loads(json.dumps(kwargs, default=str)))
        out = responses[min(len(asked), len(responses)) - 1]
        if isinstance(out, BaseException):
            raise out
        return out

    monkeypatch.setattr(llm, "_anthropic_create", create)
    return asked


def _text(text: str):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason="end_turn",
                           usage=SimpleNamespace(input_tokens=10, output_tokens=5))


def _tool_answer(value: dict):
    return SimpleNamespace(content=[SimpleNamespace(type="tool_use", name="answer", input=value, id="toolu_1")],
                           stop_reason="tool_use", usage=SimpleNamespace(input_tokens=10, output_tokens=5))


def _refused(message: str):
    import anthropic
    import httpx

    response = httpx.Response(400, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))
    return anthropic.BadRequestError(message, response=response, body={"error": {"message": message}})


async def _claude(model: str, *, schema=LEADS, notes=None, reasoning=None):
    return await llm.call_anthropic(model, [{"role": "user", "content": "Find the leads."}], "sys", "sk-ant-test",
                                    reasoning=reasoning, response_schema=schema, answer_notes=notes)


class TestClaudeIsSentItsAnswerSchema:
    async def test_the_schema_rung_sends_output_config_format(self, monkeypatch):
        asked = _claude_answers(monkeypatch, _text(json.dumps(ANSWER)))
        result = await _claude("claude-sonnet-4-6")
        assert asked[0]["output_config"]["format"] == {"type": "json_schema", "schema": LEADS}
        assert json.loads(result.text) == ANSWER

    async def test_the_format_sits_beside_adaptive_thinkings_effort(self, monkeypatch):
        asked = _claude_answers(monkeypatch, _text(json.dumps(ANSWER)))
        await _claude("claude-sonnet-4-6", reasoning=SimpleNamespace(effort="medium", budget=6144))
        assert asked[0]["output_config"] == {"effort": "medium", "format": {"type": "json_schema", "schema": LEADS}}
        assert asked[0]["thinking"]["type"] == "adaptive"

    async def test_the_forced_tool_rung_sends_one_answer_tool_with_thinking_off(self, monkeypatch):
        asked = _claude_answers(monkeypatch, _tool_answer(ANSWER))
        result = await _claude("claude-3-5-haiku-20241022", reasoning=SimpleNamespace(effort="medium", budget=2048))
        sent = asked[0]
        assert [t["name"] for t in sent["tools"]] == ["answer"] and sent["tools"][0]["input_schema"] == LEADS
        assert sent["tool_choice"] == {"type": "tool", "name": "answer"}
        assert "thinking" not in sent and "format" not in (sent.get("output_config") or {})
        assert json.loads(result.text) == ANSWER, "the tool's input is the answer"

    async def test_no_declaration_sends_neither(self, monkeypatch):
        asked = _claude_answers(monkeypatch, _text("plain words"))
        await _claude("claude-sonnet-4-6", schema=None)
        assert "format" not in (asked[0].get("output_config") or {}) and "tool_choice" not in asked[0]


class TestAFormatRefusalTakesTheNextRungOnce:
    @pytest.mark.parametrize("refusal", ["Schema is too complex for compilation.",
                                         "output_config.format: not supported for this model",
                                         "Structured outputs are not available with customer-managed keys"])
    async def test_a_refused_format_is_asked_again_in_words_and_said(self, monkeypatch, refusal):
        asked = _claude_answers(monkeypatch, _refused(refusal), _text(json.dumps(ANSWER)))
        notes: list[str] = []
        result = await _claude("claude-sonnet-4-6", notes=notes)
        assert len(asked) == 2 and "format" not in (asked[1].get("output_config") or {})
        assert json.loads(result.text) == ANSWER
        assert notes and "words" in notes[0] and "schema" in notes[0].lower(), notes

    async def test_only_once(self, monkeypatch):
        asked = _claude_answers(monkeypatch, _refused("Schema is too complex for compilation."),
                                _refused("Schema is too complex for compilation."))
        with pytest.raises(llm.LLMError):
            await _claude("claude-sonnet-4-6", notes=[])
        assert len(asked) == 2

    async def test_a_refusal_of_something_else_is_not_a_step_down(self, monkeypatch):
        asked = _claude_answers(monkeypatch, _refused("invalid x-api-key"))
        with pytest.raises(llm.LLMError):
            await _claude("claude-sonnet-4-6", notes=[])
        assert len(asked) == 1


# ── Claude on Bedrock (Converse) ─────────────────────────────────────────────

def _bedrock(monkeypatch, *answers):
    asked: list[dict] = []

    class _Bedrock:
        def converse(self, **kwargs):
            asked.append(kwargs)
            out = answers[min(len(asked), len(answers)) - 1]
            if isinstance(out, BaseException):
                raise out
            return out

        def converse_stream(self, **kwargs):
            raise AssertionError("not streamed in these tests")

    monkeypatch.setattr(llm, "_make_aws_client", lambda *a, **k: _Bedrock())
    return asked


def _converse_text(text: str) -> dict:
    return {"output": {"message": {"content": [{"text": text}]}}, "usage": {"inputTokens": 3, "outputTokens": 1},
            "stopReason": "end_turn"}


def _validation(message: str):
    from botocore.exceptions import ClientError

    return ClientError({"Error": {"Code": "ValidationException", "Message": message}}, "Converse")


BEDROCK_SETTINGS = {"aws_access_key_id": "AKIA", "aws_secret_access_key": "s", "aws_region": "us-east-1"}


class TestBedrockIsSentConversesOwnField:
    async def test_the_schema_goes_as_output_config_text_format_with_a_json_string(self, monkeypatch):
        asked = _bedrock(monkeypatch, _converse_text(json.dumps(ANSWER)))
        answer = await llm.generate_response_ex("Find the leads.", "sys", "bedrock",
                                                "bedrock.us.anthropic.claude-sonnet-4-6", BEDROCK_SETTINGS,
                                                response_schema=LEADS)
        sent = asked[0]["outputConfig"]["textFormat"]
        assert sent["type"] == "json_schema" and sent["structure"]["jsonSchema"]["name"] == "answer"
        assert json.loads(sent["structure"]["jsonSchema"]["schema"]) == LEADS
        assert json.loads(answer.text) == ANSWER

    async def test_a_model_bedrock_has_no_schema_for_is_asked_in_words(self, monkeypatch):
        asked = _bedrock(monkeypatch, _converse_text(json.dumps(ANSWER)))
        await llm.generate_response_ex("Find the leads.", "sys", "bedrock", "bedrock.amazon.nova-pro-v1:0",
                                       BEDROCK_SETTINGS, response_schema=LEADS)
        assert "outputConfig" not in asked[0]

    async def test_a_too_complex_schema_is_asked_again_in_words_once(self, monkeypatch):
        asked = _bedrock(monkeypatch, _validation("Schema is too complex."), _converse_text(json.dumps(ANSWER)))
        notes: list[str] = []
        await llm.generate_response_ex("Find the leads.", "sys", "bedrock", "bedrock.us.anthropic.claude-sonnet-4-6",
                                       BEDROCK_SETTINGS, response_schema=LEADS, answer_notes=notes)
        assert len(asked) == 2 and "outputConfig" not in asked[1] and notes


# ── the gateway, Gemini, Ollama ──────────────────────────────────────────────

class TestTheOtherSchemaModes:
    async def test_the_gateway_is_sent_a_strict_json_schema(self, monkeypatch):
        sent: dict = {}

        async def v1(model, messages, system, base_url, api_key, **kw):
            sent.update(kw)
            return llm.LLMResult(json.dumps(ANSWER), 1, 1, 0, 0, "stop", (), "")

        monkeypatch.setattr(llm, "call_v1_compatible", v1)
        settings = {"model_gateway_base_url": "http://gw.example.com", "model_gateway_key": "k"}
        await llm.call_model_gateway("synapse.claude-sonnet", [{"role": "user", "content": "x"}], "sys", settings,
                                     response_schema=LEADS)
        assert sent.get("response_schema") == LEADS

    def test_gemini_is_sent_a_nested_schema_with_no_additional_properties_anywhere(self):
        def walk(node):
            if isinstance(node, dict):
                assert "additionalProperties" not in node, node
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)

        sent = llm._gemini_schema(LEADS)
        walk(sent)
        assert sent["properties"]["leads"]["items"]["properties"]["email"] == {"type": "string"}


# ── the model CLIs ───────────────────────────────────────────────────────────

class TestACliIsSentItsOwnFlag:
    def _argv(self, base, tmp_path, schema=LEADS):
        from core.providers.cli_invocation import build_cli_invocation

        return build_cli_invocation(base, None, workdir=tmp_path, system_prompt="sys", prompt="Find the leads.",
                                    environ={"PATH": "/usr/bin"}, response_schema=schema).argv

    def test_claude_takes_json_schema_inline(self, tmp_path):
        argv = self._argv("cli.claude", tmp_path)
        assert json.loads(argv[argv.index("--json-schema") + 1]) == LEADS

    def test_codex_takes_an_output_schema_file(self, tmp_path):
        argv = self._argv("cli.codex", tmp_path)
        path = argv[argv.index("--output-schema") + 1]
        assert json.loads(open(path, encoding="utf-8").read()) == LEADS and path.startswith(str(tmp_path))

    @pytest.mark.parametrize("base", ["cli.gemini", "cli.copilot"])
    def test_a_cli_with_no_flag_is_asked_in_words(self, base, tmp_path):
        argv = self._argv(base, tmp_path)
        assert "--json-schema" not in argv and "--output-schema" not in argv

    def test_no_declaration_adds_no_flag(self, tmp_path):
        argv = self._argv("cli.claude", tmp_path, schema=None)
        assert "--json-schema" not in argv


# ── the step says which rung held it ─────────────────────────────────────────

async def test_an_llm_step_says_when_its_answer_was_asked_in_words_after_a_refusal(monkeypatch):
    """A provider that stepped down wrote a note on the call; the step that asked says it as a warning."""
    from types import SimpleNamespace as NS

    from _fakes import seed as S
    from core.models_orchestration import Orchestration
    from core.orchestration.engine import OrchestrationEngine

    async def generate(prompt_msg, sys_prompt, *a, **kw):
        notes = kw.get("answer_notes")
        if notes is not None:
            notes.append("The answer was asked for in words: the provider refused its schema (too complex).")
        return json.dumps(ANSWER)

    monkeypatch.setattr(llm, "generate_response", generate)
    orch = S.make_orchestration(id="orch_f1618b_note", entry_step_id="find", steps=[
        {"id": "find", "name": "Find Leads", "type": "llm", "model": "claude-sonnet-4-6",
         "prompt_template": "Find the leads.", "output_key": "found",
         "output_fields": [{"name": "leads", "type": "list", "of": {"fields": [
             {"name": "email", "type": "text"}, {"name": "score", "type": "number", "may_be_empty": True}]}}],
         "next_step_id": None}])
    await S.seed_orchestrations([orch])
    engine = OrchestrationEngine(Orchestration.model_validate(orch),
                                 NS(agent_sessions={}, memory_store=None, tool_router={}))
    events = [e async for e in engine.run("go", run_id="run_f1618b_note")]
    warned = [e["message"] for e in events if e.get("type") == "step_warning"]
    assert any("asked for in words" in w for w in warned), warned
