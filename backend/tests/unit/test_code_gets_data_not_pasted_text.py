"""
A code runner's program gets the run's values as data, never pasted in as text (F1212, CAP3).

CAP3's stage 0 (10-29): Synapse AI planned a Tool step on `execute_python` whose set code read
`csv_content = '{{state.read_csv}}'`. The run pasted a file's text between those quotes and the script died with a
SyntaxError — and whatever is pasted into a program is part of it, so a file's or a person's words could have run.
Now no value is written into the program: the values ride in a JSON prelude, and each reference reads one — in code
as `_values[i]`, in a string literal by rebuilding the literal as an expression, in an f-string as a replacement field.
Escaping is never relied on, so no kind of literal (raw, bytes, triple-quoted, f-string text or format spec, nested)
and no value (a NUL, a lone surrogate, a trailing backslash) can carry text out into code. The builder's check
refuses a program fed a file's content from state, naming the `/data/<path>` the code should open instead.

Every check runs the program in a real Python and reads back what it holds.
"""
import json
import subprocess
import sys
import types

import pytest

from _fakes import seed as S
from core.orchestration.steps import MissingStateRef, UnsafeCodeRef, render_code, render_tool_arguments
from core.tools import VirtualTool

from tests.unit.test_tool_step_errors import _AFTER, _run, _server

#: Words that would run if they were pasted into a program rather than handed to it, for every way to end a string:
#: each quote, a triple quote, a backslash that would escape the closing quote, f-string braces and a format spec.
HOSTILE = ("'); raise SystemExit(7); ('\n\"\"\"; raise SystemExit(8); \"\"\"\n'''; raise SystemExit(9); '''\n"
           "{__import__('os').getcwd()}{x!r}{x:{y}}\\")
#: Values escaping cannot carry: a NUL, a lone surrogate, a trailing backslash, braces.
ODD = ["\x00 after a NUL", "lone \ud800 surrogate", "ends in a backslash\\", "two\\\\", "{}{{}}"]
VALUES = [HOSTILE, *ODD]

SQ, DQ = "'", '"'
STR_LITERALS = [SQ + "{}" + SQ, DQ + "{}" + DQ, SQ * 3 + "{}" + SQ * 3, DQ * 3 + "{}" + DQ * 3,
                "r" + SQ + "{}" + SQ, "r" + DQ + "{}" + DQ, "R" + SQ * 3 + "{}" + SQ * 3, "u" + SQ + "{}" + SQ]
BYTES_LITERALS = ["b" + SQ + "{}" + SQ, "rb" + SQ + "{}" + SQ, "Br" + DQ * 3 + "{}" + DQ * 3]


def _run_python(code: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=30)


def _value_of(code: str, var: str = "x"):
    """Run the program and read back `var` exactly (as `ascii()` of it, so a NUL or a surrogate survives the pipe)."""
    out = _run_python(code + f"\nprint(ascii({var}))")
    assert out.returncode == 0, out.stderr
    return eval(out.stdout.strip())  # noqa: S307 — our own subprocess's ascii() of a str or bytes


def _ref(literal: str) -> str:
    return "x = " + literal.replace("{}", "{{state.v}}")


class TestAValueInAStringIsData:
    @pytest.mark.parametrize("literal", STR_LITERALS)
    @pytest.mark.parametrize("value", VALUES)
    def test_every_str_literal_kind(self, literal, value):
        assert _value_of(render_code(_ref(literal), {"v": value})) == value

    @pytest.mark.parametrize("literal", BYTES_LITERALS)
    @pytest.mark.parametrize("value", VALUES)
    def test_bytes(self, literal, value):
        assert _value_of(render_code(_ref(literal), {"v": value})) == value.encode("utf-8", "surrogatepass")

    @pytest.mark.parametrize("value", VALUES)
    def test_with_text_around_it_and_strings_beside_it(self, value):
        code = "x = 'Order ' 'is {{state.v}} now' " + DQ + "!" + DQ
        assert _value_of(render_code(code, {"v": value})) == f"Order is {value} now!"

    def test_a_simple_value_inside_text_reads_as_it_did(self):
        out = _run_python(render_code("print('Order {{state.order}} is late')", {"order": "SO-2077"}))
        assert out.stdout.strip() == "Order SO-2077 is late"

    def test_stage_0s_own_step(self):
        """The CSV a vault read carried, between quotes: valid Python now, holding exactly the text."""
        content = json.dumps({"path": "cap3/sales.csv", "content": "date,region\n2026-10-19,North\n"})
        code = "from io import StringIO\ncsv_content = '{{state.read_csv}}'"
        assert _value_of(render_code(code, {"read_csv": content}), "csv_content") == content


class TestAValueInAnFStringIsAField:
    @pytest.mark.parametrize("value", VALUES)
    def test_its_text(self, value):
        code = "n = 2\nx = f'{n} {{state.v}} {n!r}'"
        assert _value_of(render_code(code, {"v": value})) == f"2 {value} 2"

    def test_a_format_spec(self):
        code = "n = 3.14159\nx = f'{n:{{state.spec}}}'"
        assert _value_of(render_code(code, {"spec": ".2f"})) == "3.14"

    def test_a_hostile_format_spec_is_a_spec_not_code(self):
        out = _run_python(render_code("n = 1\nx = f'{n:{{state.spec}}}'", {"spec": "}{__import__('os').getcwd()"}))
        assert "ValueError" in out.stderr, "a bad spec fails as a spec, and runs nothing"

    @pytest.mark.parametrize("value", VALUES)
    def test_nested_f_strings(self, value):
        code = "x = f" + DQ + "<{f'[{{state.v}}]'}>" + DQ
        assert _value_of(render_code(code, {"v": value})) == f"<[{value}]>"

    def test_an_expression_inside_its_braces(self):
        assert _value_of(render_code("x = f'{ {{state.n}} + 1 }'", {"n": 41})) == "42"


class TestAnFStringThatIsOneToken:
    """Before 3.12 (PEP 701) an f-string is one STRING token, braces and all: it is filled through its tree, with the
    same answers as 3.12's f-string tokens give (F1437; 3.11 is the publish gate's Python)."""

    @pytest.mark.parametrize("value", VALUES)
    def test_a_key_in_a_fields_string(self, value):
        code = "d = {{state.d}}\nx = f" + DQ + "<{d['{{state.v}}']}>" + DQ
        assert _value_of(render_code(code, {"v": value, "d": {value: "hit"}})) == "<hit>"

    def test_the_hostile_value_in_its_text_is_never_in_the_program(self):
        out = render_code("x = f'<{{state.v}}>'", {"v": HOSTILE})
        assert "SystemExit" not in out.split("\n", 2)[2]
        assert _value_of(out) == f"<{HOSTILE}>"

    @pytest.mark.parametrize("value", VALUES)
    def test_beside_a_plain_string(self, value):
        assert _value_of(render_code("x = 'a {{state.v}}' f'-{{state.v}}-'", {"v": value})) == f"a {value}-{value}-"

    @pytest.mark.parametrize("value", VALUES)
    def test_raw_with_backslashes(self, value):
        assert _value_of(render_code(r"x = rf'\d {{state.v}} \n'", {"v": value})) == "\\d " + value + " \\n"

    @pytest.mark.parametrize("value", VALUES)
    def test_triple_quoted_over_two_lines(self, value):
        assert _value_of(render_code("x = f'''a\n{{state.v}} {1 + 1}'''", {"v": value})) == f"a\n{value} 2"

    @pytest.mark.parametrize("value", VALUES)
    def test_bytes_in_a_field(self, value):
        code = "x = f" + DQ + "{b'{{state.v}}'}" + DQ
        assert _value_of(render_code(code, {"v": value})) == str(value.encode("utf-8", "surrogatepass"))

    @pytest.mark.parametrize("value", VALUES)
    def test_a_conversion(self, value):
        assert _value_of(render_code("x = f'{ {{state.v}} !r}'", {"v": value})) == repr(value)


class TestAValueAnywhereElseIsData:
    def test_a_number_is_a_number(self):
        assert _value_of(render_code("x = {{state.n}} + 1", {"n": 41})) == 42

    def test_an_object_is_an_object(self):
        code = "rows = {{state.rows}}\nx = sum(r['amount'] for r in rows)"
        assert _value_of(render_code(code, {"rows": [{"amount": 1.5}, {"amount": 2}]})) == 3.5

    @pytest.mark.parametrize("value", [*VALUES, "__import__('os').getcwd()"])
    def test_words_are_never_code(self, value):
        assert _value_of(render_code("x = {{state.v}}", {"v": value})) == value

    def test_in_a_comment_it_is_its_name(self):
        assert "# from state.v" in render_code("# from {{state.v}}\nx = 1", {"v": HOSTILE})


class TestNothingIsEverPastedAsText:
    def test_code_that_does_not_compile_gets_data_and_the_runner_says_why(self):
        out = render_code("def broken(:\n  x = '{{state.v}}'", {"v": "raise SystemExit(9)"})
        body = out.split("\n", 2)[2]
        assert "raise SystemExit(9)" not in body and "_values[0]" in body
        assert "SyntaxError" in _run_python(out).stderr

    def test_a_place_no_expression_may_stand_is_refused(self):
        code = "x = 'a'\nmatch x:\n    case '{{state.v}}':\n        pass"
        with pytest.raises(UnsafeCodeRef):
            render_code(code, {"v": "a"})


class TestWhatStaysAsItWas:
    def test_code_with_no_reference_is_unchanged(self):
        code = "print('hello')"
        assert render_code(code, {}) == code

    def test_a_reference_to_nothing_still_says_so(self):
        with pytest.raises(MissingStateRef):
            render_code("print('{{state.nowhere}}')", {})

    def test_other_tools_arguments_are_rendered_as_before(self):
        args = {"to": "{{state.who}}", "note": "for {{state.who}}"}
        assert render_tool_arguments(args, {"who": "Priya"}) == {"to": "Priya", "note": "for Priya"}

    def test_only_a_code_runners_code(self):
        args = {"code": "x = '{{state.v}}'", "timeout": "{{state.t}}"}
        out = render_tool_arguments(args, {"v": "x'y", "t": 30}, code_tool=True)
        assert out["timeout"] == 30
        assert _value_of(out["code"]) == "x'y"


# ── the engine: a Tool step on execute_python hands its code the data ────────────────────────────────────────────
class _Session:
    def __init__(self):
        self.calls: list[dict] = []

    async def call_tool(self, name, args, **_kw):
        self.calls.append(args)
        return types.SimpleNamespace(content=[types.SimpleNamespace(text=json.dumps({"exit_code": 0}))],
                                     isError=False)


async def test_a_tool_step_on_execute_python_sends_code_that_holds_the_value(monkeypatch):
    import core.tools as tools_mod

    tool = VirtualTool("execute_python", "runs code", {"type": "object", "properties": {"code": {"type": "string"}}})

    async def one(server_module, name, agent, custom):
        return tool

    monkeypatch.setattr(tools_mod, "resolve_one_tool", one)
    session = _Session()
    server = _server()
    server.agent_sessions = {"vault_sandbox": session}
    server.tool_router = {"execute_python": ("vault_sandbox", "execute_python")}
    read = {"id": "read", "name": "Read", "type": "print", "print_content": "a 'quoted' line\nand \"more\"\\",
            "output_key": "read_csv", "next_step_id": "call"}
    call = {"id": "call", "name": "Analyse", "type": "tool", "forced_tool": "execute_python",
            "tool_arguments": {"code": "csv_content = '{{state.read_csv}}'"},
            "output_key": "result", "next_step_id": "after"}
    _, done = await _run(S.make_orchestration(entry_step_id="read", steps=[read, call, _AFTER]), server)
    assert done["status"] == "completed"
    assert _value_of(session.calls[0]["code"], "csv_content") == done["final_state"]["read_csv"]


# ── the builder's check: a file's content is not carried into code ───────────────────────────────────────────────
def _plan(code: str) -> dict:
    return {"name": "Sales", "entry_step_id": "read", "steps": [
        {"id": "read", "name": "Read", "type": "tool", "forced_tool": "vault_read",
         "tool_arguments": {"path": "cap3/sales.csv"}, "output_key": "read_csv", "next_step_id": "sum"},
        {"id": "sum", "name": "Sum", "type": "tool", "forced_tool": "execute_python",
         "tool_arguments": {"code": code}, "output_key": "totals", "next_step_id": "end"},
        {"id": "end", "name": "End", "type": "end"}]}


def test_the_check_refuses_a_files_content_carried_into_code_and_names_the_path():
    from core.builder_tools import _validate_orchestration

    out = _validate_orchestration(_plan("csv = '{{state.read_csv}}'"))
    said = " ".join(out["issues"])
    assert not out["ok"]
    assert "'/data/cap3/sales.csv'" in said, said


def test_the_check_takes_code_that_opens_the_file_itself():
    from core.builder_tools import _validate_orchestration

    out = _validate_orchestration(_plan("import pandas as pd\nprint(pd.read_csv('/data/cap3/sales.csv'))"))
    assert not any("/data/" in i for i in out["issues"]), out["issues"]
