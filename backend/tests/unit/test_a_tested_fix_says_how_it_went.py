"""A press of "Test this fix" says how the test went, and never a secret (F1380, beside F1371; eb 11-04).

F1325's card runs a Python tool's fix on its sample inputs with the internet (`test_code_change`), and its answer was
`{passed, results, next}` with nothing to say: the card's words were "Done." on either host, so the person never
read whether the fix passed. Now it says so: on a pass, what each sample printed and to press Go ahead; on a failure,
what it was given, the error's last line, and what to do next. What it says and what it answers go through the same
masking a run's events do, and more, since a press is not a run: each of the tool's own variables' values by its
name (what a traceback prints), a credential's shape, and the workspace's held secrets.
"""
from __future__ import annotations

import json

import pytest

from core import builder_tools

KEY = "sk_live_" + "4f9a2c" + "77e1b03d"
CODE = "import json\nprint(json.dumps({'total': round(sum(_args['rows']), 2)}))\n"


@pytest.fixture
def fix(monkeypatch):
    """A change whose fix is `CODE` for region_totals, which reads the workspace variable SHOP_KEY."""
    async def change_fix(args):
        return {"call": {"args": {"code": CODE}}, "target": {"kind": "tool", "name": "region_totals"},
                "fields": {"test_args": [{"rows": [1.234, 2.0]}, {"rows": [5]}]}}, None

    async def tools():
        return [{"name": "region_totals", "tool_type": "python", "globals": ["SHOP_KEY"], "code": CODE}]

    async def variables():
        return {"SHOP_KEY": KEY, "OTHER": "never read"}

    async def kept(code, samples):
        return None

    import core.store.resources as resources
    import core.variables as variables_mod
    from core import python_edits

    monkeypatch.setattr(builder_tools, "_change_fix", change_fix)
    monkeypatch.setattr(resources, "load_tools", tools)
    monkeypatch.setattr(variables_mod, "load", variables)
    monkeypatch.setattr(python_edits, "keep_pass", kept)
    box: dict = {}

    async def samples(code, globals_, given, network):
        return box["results"]

    monkeypatch.setattr(python_edits, "run_samples", samples)
    return box


async def _pressed() -> dict:
    raw, _ = await builder_tools.execute_builder_tool_with_events(
        "test_code_change", {"run_id": "builder_run_1", "op": 0}, object())
    return json.loads(raw)


class TestItSays:
    async def test_a_pass_says_what_each_sample_printed_and_to_press_go_ahead(self, fix):
        fix["results"] = [{"args": {"rows": [1.234, 2.0]}, "ok": True, "printed": '{"total": 3.23}'},
                          {"args": {"rows": [5]}, "ok": True, "printed": '{"total": 5}'}]
        said = (await _pressed()).get("say") or ""
        assert "passed on its 2 samples" in said, said
        assert 'printed {"total": 3.23}' in said and "Go ahead" in said, said

    async def test_a_failure_says_what_it_was_given_its_last_line_and_what_to_do(self, fix):
        fix["results"] = [{"args": {"rows": [5]}, "ok": False, "printed": "",
                           "error": "Traceback (most recent call last):\n  File \"<code>\", line 2\nTypeError: x"}]
        said = (await _pressed()).get("say") or ""
        assert 'failed with {"rows": [5]}' in said and "TypeError: x" in said, said
        assert "Say what to change, or stop the change" in said, said


class TestItNeverSaysASecret:
    async def test_a_variables_value_in_a_traceback_is_said_by_its_name(self, fix):
        fix["results"] = [{"args": {"rows": [5]}, "ok": False, "printed": f"calling with {KEY}",
                           "error": f"Traceback …\nValueError: bad key {KEY}"}]
        answer = await _pressed()
        whole = json.dumps(answer)
        assert KEY not in whole, whole
        assert "SHOP_KEY" in answer["say"], answer["say"]

    async def test_a_credentials_shape_is_masked_though_no_variable_holds_it(self, fix):
        token = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
        fix["results"] = [{"args": {"rows": [5]}, "ok": False, "printed": "",
                           "error": f"RuntimeError: GitHub said no to {token}"}]
        whole = json.dumps(await _pressed())
        assert token not in whole, whole
