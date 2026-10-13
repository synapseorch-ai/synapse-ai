"""A Transform that raises names your line, not the wrapper's (F407).

Measured on the fleet (C2, `p1/transform_direct.json`): a Transform `x = 1` /
`raise ValueError('direct failure')` failed its run with
`RuntimeError: Transform error: Transform failed (exit 1): Traceback (most recent
call last):\\n  File "/tmp/tmpfgnz5vym/script.py", line 17, in <module> …` — the
user's line 2 reported as line 17 (`wrap_transform` puts the state loader above
the code), the sandbox's temp path, and the engine's class name before it all.

Now the reason is one line: "Transform failed on line 2
(`raise ValueError('direct failure')`): ValueError: direct failure" — the
deepest frame in your own code, its source, and what it raised; no path, no
traceback. And a reason the engine wrote in its own words carries no class name
(Naveen, D267): any other exception keeps it ("KeyError: 'x'").
"""
import pytest

from _fakes import seed as S

CODE = "x = 1\nraise ValueError('direct failure')"


def _tb(path: str, *frames: tuple[int, str], last: str = "ValueError: direct failure") -> str:
    lines = ["Traceback (most recent call last):"]
    for line, source in frames:
        lines += [f'  File "{path}", line {line}, in <module>', f"    {source}"]
    return "\n".join(lines + [last]) + "\n"


def _failure(stderr, code=CODE, exit_code=1, label="Transform"):
    from core.execution.scripts import transform_failure, transform_line_offset
    return transform_failure(stderr, code, transform_line_offset({"a": 1}), exit_code, label=label)


class TestTheLine:
    @pytest.mark.parametrize("path", ["/sandbox/script.py", "/tmp/synapse_exec_host_ab12/script.py",
                                      "/tmp/tmpfgnz5vym/script.py"])
    async def test_is_yours_on_every_runner(self, path):
        from core.execution.scripts import transform_line_offset
        line = transform_line_offset({"a": 1}) + 2
        said = _failure(_tb(path, (line, "raise ValueError('direct failure')")))
        assert said == "Transform failed on line 2 (`raise ValueError('direct failure')`): ValueError: direct failure"

    async def test_the_offset_is_where_your_code_starts(self):
        from core.execution.scripts import transform_line_offset, wrap_transform
        state = {"note": 'a "quoted"\nvalue with """ three'}
        script = wrap_transform("FIRST_USER_LINE = 1", state).splitlines()
        assert script[transform_line_offset(state)] == "FIRST_USER_LINE = 1"

    async def test_a_missing_state_key_names_the_line_that_read_it(self):
        """The error is raised in the wrapper's state class; the line is yours."""
        from core.execution.scripts import transform_line_offset
        off = transform_line_offset({"a": 1})
        stderr = ("Traceback (most recent call last):\n"
                  f'  File "/sandbox/script.py", line {off + 1}, in <module>\n'
                  "    total = state.missing\n"
                  '  File "/sandbox/script.py", line 9, in __getattr__\n'
                  "    raise AttributeError(\n"
                  "AttributeError: no key 'missing' here; the keys are: a\n")
        said = _failure(stderr, code="total = state.missing")
        assert said == ("Transform failed on line 1 (`total = state.missing`): "
                        "AttributeError: no key 'missing' here; the keys are: a")

    async def test_inside_your_own_function_it_is_the_deepest_of_your_lines(self):
        from core.execution.scripts import transform_line_offset
        off = transform_line_offset({"a": 1})
        code = "def check(n):\n    if n < 0:\n        raise ValueError('negative')\ncheck(-1)"
        stderr = ("Traceback (most recent call last):\n"
                  f'  File "/sandbox/script.py", line {off + 4}, in <module>\n    check(-1)\n'
                  f'  File "/sandbox/script.py", line {off + 3}, in check\n'
                  "    raise ValueError('negative')\nValueError: negative\n")
        assert _failure(stderr, code=code).startswith("Transform failed on line 3 (`raise ValueError('negative')`)")

    async def test_a_syntax_error(self):
        from core.execution.scripts import transform_line_offset
        off = transform_line_offset({"a": 1})
        stderr = (f'  File "/sandbox/script.py", line {off + 2}\n    y = = 2\n        ^\n'
                  "SyntaxError: invalid syntax\n")
        assert _failure(stderr, code="x = 1\ny = = 2") == \
            "Transform failed on line 2 (`y = = 2`): SyntaxError: invalid syntax"

    async def test_a_print_before_the_traceback_is_not_the_error(self):
        from core.execution.scripts import transform_line_offset
        line = transform_line_offset({"a": 1}) + 2
        stderr = "warning: something\n" + _tb("/sandbox/script.py", (line, "raise ValueError('direct failure')"))
        assert _failure(stderr).endswith(": ValueError: direct failure")

    async def test_no_traceback_says_the_exit(self):
        assert _failure("", exit_code=137) == "Transform failed (exit 137)"
        assert _failure("Killed\n", exit_code=137) == "Transform failed (exit 137): Killed"

    async def test_the_host_runtime_keeps_its_label(self):
        from core.execution.scripts import transform_line_offset
        line = transform_line_offset({"a": 1}) + 2
        said = _failure(_tb("/tmp/x/script.py", (line, "raise ValueError('direct failure')")),
                        label="Transform (host)")
        assert said.startswith("Transform (host) failed on line 2 ")


class TestTheRun:
    async def test_its_reason_is_one_line_naming_your_line(self, monkeypatch):
        import core.config as config
        from core.models_orchestration import Orchestration
        from core.orchestration.engine import OrchestrationEngine
        from core.orchestration.run_error import failure_reason
        from core.orchestration.state import SharedState
        monkeypatch.setattr(config, "load_settings", lambda: {"transform_runtime": "host", "model": "mistral"})
        orch = S.make_orchestration(id="orch_f407", entry_step_id="t", steps=[
            {"id": "t", "name": "Check totals", "type": "transform", "transform_code": CODE,
             "output_key": "out", "timeout_seconds": 30, "next_step_id": None}])
        await S.seed_orchestrations([orch])
        [e async for e in OrchestrationEngine(Orchestration.model_validate(orch), _server()).run(
            "go", run_id="run_f407")]

        run = (await SharedState.restore("run_f407")).run
        reason = failure_reason(run.status, run.step_history, run.step_progress)
        assert reason == ("Transform error: Transform (host) failed on line 2 "
                          "(`raise ValueError('direct failure')`): ValueError: direct failure")
        assert "\n" not in reason and "/tmp" not in reason and "Traceback" not in reason


class TestTheClassName:
    async def test_the_engines_own_sentence_carries_none(self):
        from core.orchestration.run_error import reason_of
        assert reason_of(RuntimeError("Step 'Middle' timed out after 300s")) == "Step 'Middle' timed out after 300s"

    async def test_any_other_exception_keeps_it(self):
        from core.llm_providers import EmptyAnswer, LLMError
        from core.orchestration.run_error import reason_of
        assert reason_of(KeyError("missing_config_key")) == "KeyError: 'missing_config_key'"
        assert reason_of(LLMError("Claude LLM Error: 400")) == "LLMError: Claude LLM Error: 400"
        assert reason_of(EmptyAnswer("Error: empty")) == "EmptyAnswer: Error: empty"

    async def test_it_is_bounded(self):
        from core.orchestration.run_error import reason_of
        assert reason_of(RuntimeError("x" * 1000)) == "x" * 300


def _server():
    import types
    return types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})
