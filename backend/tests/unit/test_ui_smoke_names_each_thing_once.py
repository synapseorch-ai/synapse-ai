"""scripts/ui-smoke.py names each function once (F1180, F1181).

A second `def` of a name silently replaces the first: two `check_firstrun`s left F182's (a page no model can answer
says so) unrun from 10-21 on, since `check()` keyed both as "firstrun"; and two `run_to_end`s gave every caller the
later one. ui-smoke isn't in the Actions gate, so this reads the file here, without importing it or starting a
browser.
"""
from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "ui-smoke.py"


def _module_functions() -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    return [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def test_no_function_is_defined_twice():
    twice = {name: n for name, n in Counter(f.name for f in _module_functions()).items() if n > 1}
    assert twice == {}, f"a later def replaces the earlier: {twice}"


def test_no_two_checks_share_a_name():
    # `check()` keys a check by its name without `check_`, so `check_x` and `x` would collide too.
    names = [f.name.removeprefix("check_") for f in _module_functions()
             if any(isinstance(d, ast.Name) and d.id == "check" for d in f.decorator_list)]
    twice = {name: n for name, n in Counter(names).items() if n > 1}
    assert twice == {}, f"only the last of each runs: {twice}"


def test_check_refuses_a_second_check_of_one_name():
    source = SCRIPT.read_text(encoding="utf-8")
    body = source[source.index("def check(fn):"):].split("\n\n", 1)[0]
    assert "in CHECKS" in body and "raise" in body, "check() must refuse a name it already holds"
