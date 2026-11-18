"""A claude CLI answer that cannot fit its declared shape is asked for again in words, never refused as a tool (F1665).

An LLM step's answer fields reach the claude CLI as `--json-schema` (F1618), which it runs under `--max-turns 1`
(F1320) with no tool of its own (`--tools ""`). When the model's honest answer does not fit the shape (a required
number for a message that gives none), the CLI makes it answer again, which is a turn the cap does not allow: it ends
`error_max_turns` with no `structured_output`, no denial and "Reached maximum number of turns (1)" (read on the CLI,
2.1.312, Haiku 4.5 and Sonnet 4.6 alike). That was read as the model reaching for a tool of its own, so the step
failed with "ran its own tool". A second turn is no fix: the CLI then makes the model invent a value (0) to fit.

Now such a call is a refusal of the format: it is asked again in words, once, as a CLI too old for its flag is, and
the step says so. F1618's reading then types the words, and a field the answer cannot fill is asked for once more and
then named in the step's failure. The cap stays one turn.

Near misses: an answer that fits at once is read as before; a call with no schema that runs out of turns is still
refused as the model reaching for a tool; a denied tool is still refused; a schema call that fails another way is not
asked again; and the words are asked for once only.
"""
from __future__ import annotations

import json
from types import SimpleNamespace as NS

import pytest

import core.llm_providers as llm
from _fakes.cli_process import fake_cli_process

MODEL = "cli.claude.claude-haiku-4-5"
#: A required number: the shape a plan declared for an order number a message may not give.
STRICT = {"type": "object", "properties": {"is_complaint": {"type": "boolean"}, "order_number": {"type": "number"}},
          "required": ["is_complaint", "order_number"], "additionalProperties": False}
WORDS = {"is_complaint": False, "order_number": None}
#: The real call, kept before the suite's stand-in model replaces it, so the step below reaches the CLI it fakes.
REAL_GENERATE = llm.generate_response


def _result(**over) -> bytes:
    out = {"type": "result", "subtype": "success", "is_error": False, "num_turns": 1, "stop_reason": "end_turn",
           "permission_denials": [], "result": "", "session_id": "s-f1665"}
    out.update(over)
    return json.dumps(out).encode()


#: What the CLI printed for the strict shape and a message with no order number, under --max-turns 1.
RAN_OUT = _result(subtype="error_max_turns", is_error=True, num_turns=2, stop_reason="tool_use",
                  terminal_reason="max_turns", errors=["Reached maximum number of turns (1)"])
#: The same ask in words: the model's honest answer.
IN_WORDS = _result(result=json.dumps(WORDS))
#: An answer that fits at once: the structured output after the CLI's own turn to give it.
FITS = _result(num_turns=2, stop_reason="tool_use", result=json.dumps({"is_complaint": True, "order_number": 1042}),
               structured_output={"is_complaint": True, "order_number": 1042})


class _Proc:
    def __init__(self, stdout: bytes, returncode: int):
        self._stdout, self.returncode = stdout, returncode

    async def communicate(self):
        return self._stdout, b""


@pytest.fixture
def cli(monkeypatch):
    """The `claude` binary, faked: each spawn's argv is recorded, and `answer(argv)` says what it prints."""
    seen: dict = {"spawns": [], "answer": lambda argv: (IN_WORDS, 0)}

    async def fake_exec(executable, *argv, **kwargs):
        seen["spawns"].append(list(argv))
        out, code = seen["answer"](list(argv))
        return _Proc(out, code)

    monkeypatch.setattr("shutil.which", lambda cmd: f"/usr/bin/{cmd}")
    fake_cli_process(monkeypatch, fake_exec)
    return seen


def _schema_then_words(schema_says: bytes, words_say: bytes = IN_WORDS):
    return lambda argv: ((schema_says, 1 if b'"is_error": true' in schema_says else 0) if "--json-schema" in argv
                         else (words_say, 0))


def _turns(argv) -> str:
    return argv[argv.index("--max-turns") + 1]


class TestTheCall:
    async def test_an_answer_that_cannot_fit_is_asked_again_in_words_and_said(self, cli):
        cli["answer"] = _schema_then_words(RAN_OUT)
        notes: list = []
        text, _, _ = await llm.call_cli_provider(MODEL, "Classify: Do you ship to Madeira?", response_schema=STRICT,
                                                 answer_notes=notes)
        assert json.loads(text) == WORDS
        first, second = cli["spawns"]
        assert "--json-schema" in first and "--json-schema" not in second
        assert _turns(first) == _turns(second) == "1", "the cap stays one turn: a second invents a value to fit"
        assert len(notes) == 1 and "asked for in words" in notes[0], notes

    async def test_one_that_fits_at_once_is_read_as_before(self, cli):
        cli["answer"] = _schema_then_words(FITS)
        notes: list = []
        text, _, _ = await llm.call_cli_provider(MODEL, "Classify: order 1042 is broken.", response_schema=STRICT,
                                                 answer_notes=notes)
        assert json.loads(text) == {"is_complaint": True, "order_number": 1042}
        assert len(cli["spawns"]) == 1 and notes == []

    async def test_without_a_schema_running_out_of_turns_is_still_refused_as_a_tool(self, cli):
        cli["answer"] = lambda argv: (RAN_OUT, 1)
        with pytest.raises(llm.LLMError, match="ran its own tool"):
            await llm.call_cli_provider(MODEL, "Classify: Do you ship to Madeira?")
        assert len(cli["spawns"]) == 1

    async def test_a_denied_tool_is_still_refused(self, cli):
        cli["answer"] = _schema_then_words(_result(permission_denials=[{"tool_name": "Bash"}]))
        with pytest.raises(llm.LLMError, match="ran its own tool"):
            await llm.call_cli_provider(MODEL, "Classify it.", response_schema=STRICT, answer_notes=[])
        assert len(cli["spawns"]) == 1

    async def test_a_schema_call_that_fails_another_way_is_not_asked_again(self, cli):
        cli["answer"] = _schema_then_words(_result(subtype="error_during_execution", is_error=True,
                                                   result="the API answered 500"))
        with pytest.raises(llm.LLMError, match="failed"):
            await llm.call_cli_provider(MODEL, "Classify it.", response_schema=STRICT, answer_notes=[])
        assert len(cli["spawns"]) == 1

    async def test_the_words_are_asked_for_once_only(self, cli):
        cli["answer"] = lambda argv: (RAN_OUT, 1)
        with pytest.raises(llm.LLMError):
            await llm.call_cli_provider(MODEL, "Classify it.", response_schema=STRICT, answer_notes=[])
        assert len(cli["spawns"]) == 2


async def test_a_step_whose_field_cannot_be_filled_fails_naming_it_never_as_a_tool(cli, monkeypatch):
    """End to end: the step is asked in words, its answer typed, repaired once, and its failure names the field."""
    from _fakes import seed as S
    from core.models_orchestration import Orchestration
    from core.orchestration.engine import OrchestrationEngine

    monkeypatch.setattr(llm, "generate_response", REAL_GENERATE)
    cli["answer"] = _schema_then_words(RAN_OUT)
    orch = S.make_orchestration(id="orch_f1665", entry_step_id="classify", steps=[
        {"id": "classify", "name": "Classify", "type": "llm", "model": MODEL,
         "prompt_template": "Classify this message: {{ state.user_input }}", "output_key": "classify",
         "output_fields": [{"name": "is_complaint", "type": "boolean"}, {"name": "order_number", "type": "number"}],
         "next_step_id": None}])
    await S.seed_orchestrations([orch])
    engine = OrchestrationEngine(Orchestration.model_validate(orch),
                                 NS(agent_sessions={}, memory_store=None, tool_router={}))
    events = [e async for e in engine.run("Do you ship to Madeira?", run_id="run_f1665")]
    said = json.dumps(events, default=str)
    warned = [e["message"] for e in events if e.get("type") == "step_warning"]
    assert any("asked for in words" in w for w in warned), warned
    assert "ran its own tool" not in said
    failed = [e for e in events if e.get("type") == "step_error"]
    assert failed and "`order_number` is null, not a number" in failed[0]["reason"], failed
