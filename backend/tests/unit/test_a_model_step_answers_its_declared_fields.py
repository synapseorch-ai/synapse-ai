"""A model step declares its answer's fields, and the run holds the model to them — F1553 (BLD4; Naveen, 11-12).

Naveen: "Sometimes the builder just builds things with Extract JSON and If/Else. But Extract JSON only extracts if
JSON exists in the input of the step." BLD4's triage (26 builds on Sonnet 4.6 and Haiku 4.5, 50 runs): 17 of 19
decisions on a model's answer were code reading the model's free text — Extract JSON then a field, a field read
straight off the answer, a hand-written parse, a one-word label read as text — and a miss took the If/Else's False
path with the run Completed: a 2-star review got the thank-you-and-referral email, a safe contract clause went to
legal, two parse Transforms defaulted to "not risky" and "auto-approve". Nothing could declare what the model answers:
`llm` and `agent` steps had no output schema, no provider call asked for JSON, and F1549's field check left every
model's answer alone.

Naveen's pick, (a): an `llm` or `agent` step declares `output_fields`. The engine asks the model for that one object
(natively where the provider has a JSON schema mode: OpenAI, Gemini, Ollama; in words everywhere), reads it the way
Extract JSON reads text, coerces each field to its type, and on a miss asks **the step's own model** once more with
its own answer; still a miss, the step **fails** naming the field — never a branch on a guess. The validator refuses a
condition, a template or a Transform reading a field the model step does not declare (F1549's check, extended). The
builder declares fields and plans no new Extract JSON step; an existing Extract JSON step that finds no JSON in text
now fails instead of storing null (empty input is unchanged). The editor's LLM and Agent panels set the fields.
"""
from __future__ import annotations

import json
import types
from pathlib import Path

import pytest
from _fakes import seed as S

from core.builder_tools import _validate_orchestration

ROOT = Path(__file__).resolve().parents[3]

RISKY = {"name": "risky", "type": "boolean", "description": "Whether the change is risky"}
REASONS = {"name": "reasons", "type": "list", "description": "Each risk found"}
FIT = {"name": "fit", "type": "choice", "options": ["good", "poor"]}
SCORE = {"name": "score", "type": "number"}


# ── The contract ───────────────────────────────────────────────────────────────────────────────────────────────────

class TestTheContract:
    def test_json_inside_prose_and_a_fence_is_read_and_typed(self):
        from core.orchestration import output_fields as OF

        text = 'Looking at this diff, two problems.\n\n```json\n{"risky": "true", "reasons": ["drops a table"], ' \
               '"extra": 1}\n```\nBlock it.'
        answer, problems = OF.read([RISKY, REASONS], text)
        assert problems == [] and answer == {"risky": True, "reasons": ["drops a table"]}, (answer, problems)

    @pytest.mark.parametrize("field,given,stored", [
        (RISKY, "Yes", True), (RISKY, "false", False), (RISKY, True, True), (RISKY, "approve", True),
        (SCORE, "7", 7), (SCORE, "7.5", 7.5), (SCORE, 3, 3),
        (FIT, "Good", "good"), (FIT, " poor ", "poor"),
        (REASONS, ["a", "b"], ["a", "b"]), (REASONS, "one reason", ["one reason"]),
        ({"name": "summary", "type": "text"}, 5, "5"),
    ])
    def test_each_type_is_coerced(self, field, given, stored):
        from core.orchestration import output_fields as OF

        answer, problems = OF.read([field], json.dumps({field["name"]: given}))
        assert problems == [] and answer == {field["name"]: stored}, (answer, problems)

    @pytest.mark.parametrize("fields,text,named", [
        ([RISKY], "The change looks risky: it drops the customers table.", "risky"),
        ([RISKY, REASONS], '{"reasons": []}', "risky"),
        ([RISKY], '{"risky": "maybe"}', "risky"),
        ([FIT], '{"fit": "great"}', "fit"),
        ([SCORE], '{"score": "high"}', "score"),
    ])
    def test_a_miss_names_the_field(self, fields, text, named):
        from core.orchestration import output_fields as OF

        answer, problems = OF.read(fields, text)
        assert answer is None and any(f"`{named}`" in p for p in problems), (answer, problems)

    def test_its_json_schema(self):
        from core.orchestration import output_fields as OF

        schema = OF.json_schema([RISKY, REASONS, FIT, SCORE, {"name": "note", "type": "textarea"}])
        assert schema["type"] == "object" and schema["additionalProperties"] is False
        assert schema["required"] == ["risky", "reasons", "fit", "score", "note"]
        props = schema["properties"]
        assert props["risky"]["type"] == "boolean" and props["score"]["type"] == "number"
        assert props["fit"] == {"type": "string", "enum": ["good", "poor"]}
        assert props["reasons"]["type"] == "array" and props["reasons"]["items"] == {"type": "string"}
        assert props["note"]["type"] == "string" and props["risky"]["description"] == "Whether the change is risky"

    def test_the_instruction_names_every_field_and_its_type(self):
        from core.orchestration import output_fields as OF

        said = OF.instruction([RISKY, FIT, REASONS])
        assert "JSON object" in said
        for words in ("`risky`", "true or false", "`fit`", '"good"', '"poor"', "`reasons`", "list",
                      "Whether the change is risky"):
            assert words in said, (words, said)

    @pytest.mark.parametrize("fields,said", [
        ([{"name": "is risky", "type": "boolean"}], "not a name"),
        ([RISKY, RISKY], "twice"),
        ([{"name": "x", "type": "vector"}], "type"),
        ([{"name": "fit", "type": "choice"}], "options"),
    ])
    def test_a_declaration_that_cannot_be_held(self, fields, said):
        from core.orchestration import output_fields as OF

        problems = OF.declaration_problems(fields)
        assert problems and any(said in p for p in problems), problems
        assert OF.declaration_problems([RISKY, FIT, REASONS, SCORE]) == []


class TestTheStepSchema:
    @pytest.mark.parametrize("kind", ["llm", "agent"])
    def test_a_model_step_takes_them(self, kind):
        from core.models_orchestration import StepConfig

        step = StepConfig(id="s", name="S", type=kind, output_fields=[RISKY])
        assert step.output_fields == [RISKY]

    def test_no_other_step_does(self):
        from pydantic import ValidationError

        from core.models_orchestration import StepConfig

        with pytest.raises(ValidationError, match="output_fields"):
            StepConfig(id="s", name="S", type="print", output_fields=[RISKY])

    def test_never_beside_finish_tools(self):
        from pydantic import ValidationError

        from core.models_orchestration import StepConfig

        with pytest.raises(ValidationError, match="output_fields"):
            StepConfig(id="s", name="S", type="agent", finish_tools=["submit"], output_fields=[RISKY])


# ── The engine ─────────────────────────────────────────────────────────────────────────────────────────────────────

def _server():
    return types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})


def _print(sid: str) -> dict:
    return {"id": sid, "name": sid.upper(), "type": "print", "print_content": sid, "output_key": f"o_{sid}",
            "next_step_id": None}


def _reviewer(orch_id: str, fields: list[dict], condition: str = "state.analyze.risky") -> dict:
    """The PR reviewer BLD4's Sonnet build made, its Extract JSON step folded into the model step's fields."""
    return S.make_orchestration(id=orch_id, entry_step_id="analyze", steps=[
        {"id": "analyze", "name": "Analyze Diff", "type": "llm", "model": "gpt-4o",
         "prompt_template": "Is this diff risky?\n\n{{ state.user_input }}", "output_key": "analyze",
         "output_fields": fields, "next_step_id": "route"},
        {"id": "route", "name": "Risky?", "type": "if_else", "if_condition": condition,
         "if_true_step_id": "block", "if_false_step_id": "approve"},
        _print("block"), _print("approve"),
    ])


async def _ran(orch: dict, message: str = "diff --git a/x b/x"):
    from core.models_orchestration import Orchestration
    from core.orchestration.engine import OrchestrationEngine

    await S.seed_orchestrations([orch])
    engine = OrchestrationEngine(Orchestration.model_validate(orch), _server())
    events = [e async for e in engine.run(message, run_id=f"run_{orch['id']}")]
    return events


def _decided(events) -> list[str]:
    return [e["result"] for e in events if e.get("type") == "if_decision"]


def _failed(events) -> str:
    said = " ".join(str(e.get("error") or e.get("response") or e.get("message") or "") for e in events
                    if e.get("type") in ("error", "orchestration_error", "step_error", "final")
                    and ("failed" in str(e).lower() or e.get("type") != "final"))
    return said


class TestAnLlmStep:
    async def test_its_json_answer_is_the_object_a_condition_reads(self, fake_llm):
        fake_llm.script(['```json\n{"risky": true, "reasons": ["drops a table"]}\n```'])
        events = await _ran(_reviewer("orch_f1553_json", [RISKY, REASONS]))
        assert _decided(events) == ["true"]
        assert len(fake_llm.calls) == 1, "no repair when it answered"

    async def test_a_string_false_is_false(self, fake_llm):
        """`{"risky": "false"}` was truthy under a bare condition: the approve arm only by luck of the wording."""
        fake_llm.script(['{"risky": "false", "reasons": []}'])
        assert _decided(await _ran(_reviewer("orch_f1553_strfalse", [RISKY, REASONS]))) == ["false"]

    async def test_the_call_asks_for_the_shape(self, fake_llm):
        fake_llm.script(['{"risky": false, "reasons": []}'])
        await _ran(_reviewer("orch_f1553_asks", [RISKY, REASONS]))
        first = fake_llm.calls[0]
        assert first.get("response_schema", {}).get("required") == ["risky", "reasons"], first.keys()
        assert "`risky`" in first["prompt_msg"] and "JSON object" in first["prompt_msg"]

    async def test_prose_is_asked_once_more_of_the_same_model(self, fake_llm):
        fake_llm.script(["The change is risky: it drops the customers table. Block it.",
                         '{"risky": true, "reasons": ["drops the customers table"]}'])
        events = await _ran(_reviewer("orch_f1553_repair", [RISKY, REASONS]))
        assert _decided(events) == ["true"]
        assert len(fake_llm.calls) == 2
        repair = fake_llm.calls[1]
        assert repair["current_model"] == fake_llm.calls[0]["current_model"] == "gpt-4o"
        assert "drops the customers table" in repair["prompt_msg"] and "`risky`" in repair["prompt_msg"]
        assert "Is this diff risky?" in repair["prompt_msg"], "the same criteria, never a second judge from text alone"
        assert any(e.get("type") == "step_warning" and "risky" in str(e.get("message")) for e in events)

    async def test_prose_twice_fails_the_step_naming_the_field(self, fake_llm):
        fake_llm.script(["It looks risky to me.", "Honestly, I would block it."])
        events = await _ran(_reviewer("orch_f1553_fails", [RISKY, REASONS]))
        assert _decided(events) == [], "no branch is taken on a guess"
        said = _failed(events)
        assert "`risky`" in said and "Analyze Diff" in said and "Replay" in said, said
        assert not any(e.get("orch_step_id") in ("block", "approve") for e in events)

    async def test_a_step_without_fields_is_as_before(self, fake_llm):
        fake_llm.script(["URGENT"])
        events = await _ran(_reviewer("orch_f1553_plain", [], condition="state.analyze.strip() == 'URGENT'"))
        assert _decided(events) == ["true"]
        assert "response_schema" not in fake_llm.calls[0] or fake_llm.calls[0]["response_schema"] is None


class TestAnAgentStep:
    @staticmethod
    def _agent_answers(monkeypatch, text: str):
        import core.react_engine as re_mod

        async def run_agent_step(**kwargs):
            run_agent_step.message = kwargs.get("message")
            yield {"type": "final", "response": text}

        monkeypatch.setattr(re_mod, "run_agent_step", run_agent_step)
        return run_agent_step

    @staticmethod
    def _orch(orch_id: str) -> dict:
        return S.make_orchestration(id=orch_id, entry_step_id="analyze", steps=[
            {"id": "analyze", "name": "Analyze for Risks", "type": "agent", "agent_id": "agent_risk",
             "model": "gpt-4o", "prompt_template": "Analyze this diff.", "output_key": "analyze",
             "output_fields": [RISKY], "next_step_id": "route"},
            {"id": "route", "name": "Risky?", "type": "if_else", "if_condition": "state.analyze.risky",
             "if_true_step_id": "block", "if_false_step_id": "approve"},
            _print("block"), _print("approve"),
        ])

    async def test_its_final_answer_is_read_and_its_prompt_asks_for_the_shape(self, monkeypatch, fake_llm):
        ran = self._agent_answers(monkeypatch, 'Looking at it.\n```json\n{"risky": true}\n```')
        assert _decided(await _ran(self._orch("orch_f1553_agent"))) == ["true"]
        assert "`risky`" in ran.message and fake_llm.calls == []

    async def test_prose_is_asked_once_more_of_its_model(self, monkeypatch, fake_llm):
        self._agent_answers(monkeypatch, "This PR drops two tables and holds a secret. Risky.")
        fake_llm.script(['{"risky": true}'])
        assert _decided(await _ran(self._orch("orch_f1553_agent_repair"))) == ["true"]
        assert len(fake_llm.calls) == 1 and fake_llm.calls[0]["current_model"] == "gpt-4o"
        assert "drops two tables" in fake_llm.calls[0]["prompt_msg"]

    async def test_prose_twice_fails(self, monkeypatch, fake_llm):
        self._agent_answers(monkeypatch, "Risky, I think.")
        fake_llm.script(["Still risky."])
        events = await _ran(self._orch("orch_f1553_agent_fails"))
        assert _decided(events) == [] and "`risky`" in _failed(events)


class TestExtractJson:
    @staticmethod
    async def _extracted(state: dict):
        from core.models_orchestration import OrchestrationRun, StepConfig
        from core.orchestration.steps import ExtractJsonStepExecutor

        step = StepConfig(id="parse", name="Parse Analysis", type="extract_json", input_keys=["analyze"],
                          output_key="parse")
        run = OrchestrationRun(run_id="run_f1553_x", orchestration_id="orch_f1553_x", shared_state=dict(state))
        events = [e async for e in ExtractJsonStepExecutor().execute(step, run, types.SimpleNamespace(
            step_map={"analyze": types.SimpleNamespace(name="Analyze Diff", output_key="analyze")}))]
        return events, run.shared_state

    async def test_text_with_no_json_fails_the_step(self):
        with pytest.raises(RuntimeError) as failed:
            await self._extracted({"analyze": "Looks risky: it drops the customers table."})
        said = str(failed.value)
        assert "no JSON" in said and "Replay" in said, said

    async def test_empty_input_is_as_before(self):
        events, state = await self._extracted({})
        assert state.get("parse") is None and any(e.get("type") == "step_warning" for e in events)

    async def test_json_is_stored_as_before(self):
        _, state = await self._extracted({"analyze": 'Here: {"risky": true}'})
        assert state["parse"] == {"risky": True}


# ── Providers ──────────────────────────────────────────────────────────────────────────────────────────────────────

SCHEMA = {"type": "object", "properties": {"risky": {"type": "boolean"}}, "required": ["risky"],
          "additionalProperties": False}


class _Resp:
    def __init__(self, status: int, body: dict | None = None, text: str = ""):
        self.status_code, self._body, self.text = status, body or {}, text or json.dumps(body or {})

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx

            raise httpx.HTTPStatusError("x", request=None, response=self)  # type: ignore[arg-type]


_OK = {"choices": [{"message": {"content": '{"risky": true}'}, "finish_reason": "stop"}],
       "usage": {"prompt_tokens": 3, "completion_tokens": 2}}


class TestProviders:
    async def test_openai_is_sent_the_schema(self, monkeypatch):
        import core.llm_providers as llm

        sent = []

        async def post(client, url, *, headers, payload):
            sent.append(json.loads(json.dumps(payload)))
            return _Resp(200, _OK)

        monkeypatch.setattr(llm, "_post_chat", post)
        await llm.call_openai("gpt-4o", [{"role": "user", "content": "x"}], "k", response_schema=SCHEMA)
        fmt = sent[0]["response_format"]
        assert fmt["type"] == "json_schema" and fmt["json_schema"]["schema"] == SCHEMA
        assert fmt["json_schema"]["strict"] is True
        sent.clear()
        await llm.call_openai("gpt-4o", [{"role": "user", "content": "x"}], "k")
        assert "response_format" not in sent[0]

    async def test_openai_that_refuses_it_is_asked_without(self, monkeypatch):
        import core.llm_providers as llm

        sent = []

        async def post(client, url, *, headers, payload):
            sent.append(dict(payload))
            if "response_format" in payload:
                return _Resp(400, {"error": {"message": "Invalid parameter: 'response_format' of type "
                                                        "'json_schema' is not supported with this model."}})
            return _Resp(200, _OK)

        monkeypatch.setattr(llm, "_post_chat", post)
        result = await llm.call_openai("gpt-3.5-turbo", [{"role": "user", "content": "x"}], "k",
                                       response_schema=SCHEMA)
        assert len(sent) == 2 and "response_format" not in sent[1]
        assert '{"risky": true}' in (result[0] if isinstance(result, tuple) else result.text)

    async def test_the_dispatch_hands_the_schema_to_openai(self, monkeypatch):
        import core.llm_providers as llm

        got = {}

        async def call_openai(model, messages, api_key, **kwargs):
            got.update(kwargs)
            return llm.LLMResult('{"risky": true}', 1, 1)

        monkeypatch.setattr(llm, "call_openai", call_openai)
        await llm._generate_response_once("x", "sys", "cloud", "gpt-4o", {"openai_key": "k"},
                                          response_schema=SCHEMA)
        assert got.get("response_schema") == SCHEMA

    async def test_gemini_is_sent_the_schema(self, monkeypatch):
        import core.llm_providers as llm

        configs = []

        class Models:
            def generate_content(self, *, model, contents, config):
                configs.append(config)
                return types.SimpleNamespace(
                    candidates=[types.SimpleNamespace(
                        content=types.SimpleNamespace(parts=[types.SimpleNamespace(
                            text='{"risky": true}', function_call=None, thought=None)]),
                        finish_reason="STOP")],
                    usage_metadata=types.SimpleNamespace(prompt_token_count=1, candidates_token_count=1,
                                                         cached_content_token_count=0, thoughts_token_count=0),
                    text='{"risky": true}')

        monkeypatch.setattr(llm, "_gemini_client_for", lambda *a, **k: types.SimpleNamespace(models=Models()))
        try:
            await llm.call_gemini("gemini-2.5-flash", [{"role": "user", "content": "x"}], "sys", "k",
                                  response_schema=SCHEMA)
        except Exception:  # noqa: BLE001 — reading the fake answer is not what is checked here
            pass
        assert configs and configs[0].response_mime_type == "application/json"
        assert configs[0].response_json_schema is not None

    async def test_ollama_is_sent_the_schema(self, monkeypatch):
        import core.llm_providers as llm

        bodies = []

        async def post(client, url, body, *, field):
            bodies.append(body)
            return llm._OllamaStreamed({"response": '{"risky": true}', "done_reason": "stop"})

        monkeypatch.setattr(llm, "_ollama_post", post)
        call = llm.ChatCall(model="ollama.qwen3", prompt="x", history=None, system="s", settings={},
                            tools=None, images=None, prompt_cache=False, reasoning=None, max_out=100,
                            sent_cap=None, agent_cap=None, prompt_blocks=None, volatile_tail=None,
                            reasoning_effort=None, response_schema=SCHEMA)
        await llm._chat_ollama(call, None)
        assert bodies[0]["format"] == SCHEMA


# ── The validator ──────────────────────────────────────────────────────────────────────────────────────────────────

def _orch(*steps: dict) -> dict:
    steps = [dict(s) for s in steps]
    for here, after in zip(steps, steps[1:]):
        if here.get("type") not in ("if_else", "switch", "end"):
            here.setdefault("next_step_id", after["id"])
    if steps[-1].get("type") != "end":
        steps[-1].setdefault("next_step_id", "end")
        steps.append({"id": "end", "name": "End", "type": "end"})
    return {"id": "orch_f1553", "name": "F1553", "entry_step_id": steps[0]["id"], "steps": steps}


def _model(kind: str = "llm", fields=None, **over) -> dict:
    step = {"id": "analyze", "name": "Analyze", "type": kind, "prompt_template": "Is it risky?",
            "output_key": "analyze", **({"output_fields": fields} if fields is not None else {}), **over}
    if kind == "agent":
        step.setdefault("agent_id", "agent_risk")
    return step


def _gate(condition: str) -> dict:
    return {"id": "route", "name": "Risky?", "type": "if_else", "if_condition": condition,
            "if_true_step_id": "after", "if_false_step_id": "end"}


AFTER = {"id": "after", "name": "After", "type": "llm", "prompt_template": "Write it.", "output_key": "after"}
PLAIN_AGENT = {"agent_risk": {"id": "agent_risk", "name": "Risk", "type": "agent", "tools": []}}


def _undeclared(orch: dict, agents=None) -> list[str]:
    return [i for i in _validate_orchestration(orch, agents=agents)["issues"] if "output_fields" in i]


class TestTheValidator:
    def test_a_condition_on_a_field_the_llm_step_does_not_declare(self):
        [said] = _undeclared(_orch(_model(), _gate("state.analyze.risky == True"), AFTER))
        assert said.index("output_fields") < said.index("or read"), "the repair first"
        assert "`risky`" in said and '"type": "boolean"' in said, said

    def test_declared_it_passes(self):
        assert _undeclared(_orch(_model(fields=[RISKY]), _gate("state.analyze.risky == True"), AFTER)) == []

    def test_a_field_it_does_not_declare_names_the_ones_it_does(self):
        [said] = _undeclared(_orch(_model(fields=[RISKY]), _gate("state.analyze.is_risky"), AFTER))
        assert "`is_risky`" in said and "`risky`" in said, said

    def test_a_template_and_a_transforms_code(self):
        templ = {**AFTER, "prompt_template": "Summary: {{ state.analyze.summary }}"}
        assert any("`summary`" in i for i in _undeclared(_orch(_model(), templ)))
        code = {"id": "after", "name": "After", "type": "transform", "output_key": "after",
                "transform_code": "result = state['analyze']['risky']"}
        assert any("`risky`" in i for i in _undeclared(_orch(_model(), code)))

    @pytest.mark.parametrize("condition", ["state.analyze.strip().upper() == 'URGENT'", "len(state.analyze) > 10",
                                           "'risky' in state.analyze"])
    def test_a_whole_read_or_a_text_method_is_left_alone(self, condition):
        assert _undeclared(_orch(_model(), _gate(condition), AFTER)) == []

    def test_an_agent_step_with_its_agent_known(self):
        orch = _orch(_model("agent"), _gate("state.analyze.risky"), AFTER)
        assert _undeclared(orch, PLAIN_AGENT), "a plain agent's answer is its words"
        assert _undeclared(orch) == [], "agents not given: not known, as before"
        assert _undeclared(_orch(_model("agent", fields=[RISKY]), _gate("state.analyze.risky"), AFTER),
                           PLAIN_AGENT) == []
        finishing = _orch(_model("agent", finish_tools=["submit"]), _gate("state.analyze.risky"), AFTER)
        assert _undeclared(finishing, {"agent_risk": {**PLAIN_AGENT["agent_risk"], "tools": ["submit"]}}) == []

    def test_a_workflow_agents_step_takes_no_fields(self):
        agents = {"agent_risk": {"id": "agent_risk", "name": "Risk flow", "type": "orchestrator",
                                 "orchestration_id": "orch_child"}}
        orch = _orch(_model("agent", fields=[RISKY]), AFTER)
        assert any("output_fields" in i for i in _validate_orchestration(orch, agents=agents)["issues"])

    def test_a_declaration_that_cannot_be_held_is_said(self):
        orch = _orch(_model(fields=[{"name": "fit", "type": "choice"}]), AFTER)
        assert any("options" in i and "output_fields" in i for i in _validate_orchestration(orch)["issues"])

    @pytest.mark.parametrize("path", sorted(str(p.relative_to(ROOT)) for p in (ROOT / "backend/examples").glob(
        "*.bundle.json")) + ["backend/core/native_builder/orchestration.json"])
    def test_the_shipped_workflows(self, path):
        doc = json.loads((ROOT / path).read_text())
        for orch in doc.get("orchestrations", [doc]):
            assert _undeclared(orch) == [], (path, orch.get("id"))

    def test_the_mcp_tools_take_them_as_json(self):
        from core.builder_tools import _normalize_step_inputs

        step = _normalize_step_inputs({"id": "a", "type": "llm", "output_fields_json": json.dumps([RISKY])})
        assert step["output_fields"] == [RISKY] and "output_fields_json" not in step


# ── The builder ────────────────────────────────────────────────────────────────────────────────────────────────────

def _plan(*steps: dict, kind: str = "create") -> dict:
    return {"kind": kind, "title": "PR review", "summary": "Reviews a diff.", "steps": list(steps)}


ANALYZE = {"id": "analyze", "type": "llm", "name": "Analyze Diff", "does": "Judges the diff",
           "prompt": "Is this diff risky?\n\n{{ state.user_input }}", "next": "route"}
ROUTE = {"id": "route", "type": "if_else", "name": "Risky?", "does": "Branches on it",
         "condition": "state.analyze.risky == True", "branches": {"true": "block", "false": "approve"}}
BLOCK = {"id": "block", "type": "llm", "name": "Block", "does": "Writes a blocking comment",
         "prompt": "Write a blocking comment.", "next": "end"}
APPROVE = {"id": "approve", "type": "llm", "name": "Approve", "does": "Writes an approving comment",
           "prompt": "Write an approving comment.", "next": "end"}


class TestTheBuilder:
    async def _errors(self, plan: dict, state: dict | None = None) -> list[str]:
        from core.native_builder import build_plan

        found = await build_plan.examine(plan, None, ws=build_plan.Workspace(),
                                         state=state or {"user_message": "x"})
        return found.errors

    async def test_a_new_extract_json_step_is_refused(self):
        parse = {"id": "parse", "type": "extract_json", "name": "Parse", "does": "Reads the JSON", "next": "route"}
        errors = await self._errors(_plan({**ANALYZE, "next": "parse"}, parse,
                                          {**ROUTE, "condition": "state.parse.risky == True"}, BLOCK, APPROVE))
        [said] = [e for e in errors if "extract_json" in e or "Extract JSON" in e]
        assert "config.output_fields" in said, said

    async def test_an_undeclared_read_comes_back_in_the_planners_terms(self):
        errors = await self._errors(_plan(ANALYZE, ROUTE, BLOCK, APPROVE))
        [said] = [e for e in errors if "output_fields" in e]
        assert "config.output_fields" in said, said

    async def test_declared_it_compiles_with_the_fields(self):
        from core.native_builder import objects as O
        from core.native_builder.compile import compile_plan

        plan = _plan({**ANALYZE, "config": {"output_fields": [RISKY]}}, ROUTE, BLOCK, APPROVE)
        assert not [e for e in await self._errors(plan) if "output_fields" in e]
        steps = {s["id"]: s for s in compile_plan(O.Plan.from_draft(plan)).orchestration["steps"]}
        assert steps["analyze"]["output_fields"] == [RISKY]


def _planner() -> str:
    from core.native_builder import prompt_lint

    return " ".join(prompt_lint.expanded_places()["agent:agent_native_builder_planner"].split())


class TestTheBuilderIsTold:
    def test_structure_rule_2_declares_fields_on_the_model_step(self):
        text = _planner()
        rule = text[text.index("# Structure"):text.index("# Changing")]
        assert "`config.output_fields`" in rule, rule
        assert "is an `extract_json` step right after it" not in rule

    def test_the_step_guide_says_a_model_step_declares_them(self):
        text = _planner()
        llm = text[text.index("- `llm`:"):text.index("- `tool`:")]
        agent = text[text.index("- `agent`:"):text.index("- `llm`:")]
        for part in (llm, agent):
            assert "`config.output_fields`" in part, part
        assert "choice" in llm and "options" in llm, llm

    def test_no_prompt_tells_the_planner_to_extract_json(self):
        from core.native_builder import AGENTS_SDK_GUIDE, KNOWLEDGE_STEPS_PALETTE

        for name, text in (("planner", _planner()), ("paste guide", AGENTS_SDK_GUIDE),
                           ("knowledge palette", KNOWLEDGE_STEPS_PALETTE)):
            flat = " ".join(text.split())
            for said in ("followed by an `extract_json` step", "then `extract_json`", "an `extract_json` step's id",
                         "put an `extract_json` step after", "is an `extract_json` step right after it"):
                assert said not in flat, (name, said)

    def test_the_paste_guide_maps_output_type_to_output_fields(self):
        from core.native_builder import AGENTS_SDK_GUIDE

        flat = " ".join(AGENTS_SDK_GUIDE.replace("\\\n", "").split())
        rule = flat[flat.index("2. **"):flat.index("3. **")]
        assert "`config.output_fields`" in rule, rule
        line = next(ln for ln in AGENTS_SDK_GUIDE.replace("\\\n", "").splitlines() if "output_type" in ln)
        assert "output_fields" in line, line


class TestTheEditor:
    def test_the_llm_and_agent_panels_set_answer_fields(self):
        panel = (ROOT / "frontend/src/components/orchestration/panel/steps.tsx").read_text()
        for section in ("function AgentSection(", "function LlmSection("):
            body = panel[panel.index(section):]
            body = body[:body.index("\n}\n")]
            assert "<AnswerFields" in body, section
        assert "output_fields" in (ROOT / "frontend/src/types/orchestration.ts").read_text()

    def test_extract_jsons_note_says_it_fails_on_no_json(self):
        panel = (ROOT / "frontend/src/components/orchestration/panel/steps.tsx").read_text()
        note = " ".join(panel[panel.index("function ExtractJsonSection("):].split())[:1500]
        assert "fails" in note and "Answer fields" in note, note
