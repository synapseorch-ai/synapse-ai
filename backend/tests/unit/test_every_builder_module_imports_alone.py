"""Every builder module imports on its own, first, in a fresh interpreter (F1293; 79, the #5 row: third time bitten).

`core.builder_tools` loads the builder's modules at its end so each registers its tools (BWA1, D274). A module that
imports `builder_tools` at load time, imported first, is still half-made while `builder_tools` loads the rest; one of
those that takes names from it (`from …build_plan import FINDINGS_KEY`, `from …change_tools import _caller`) then
found them missing, and its file only worked after another had loaded the builder. Each now takes the module and reads
its names when it runs. This holds the order: each of `builder_tools`' modules, read from its own list, and the
change modules, imported alone.
"""
from __future__ import annotations

import ast
import os
import pathlib
import subprocess
import sys

import pytest

BACKEND = pathlib.Path(__file__).resolve().parents[2]


def _registered() -> list[str]:
    """`builder_tools`' own list of the modules it loads, read from its source."""
    tree = ast.parse((BACKEND / "core" / "builder_tools.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.For) and isinstance(node.target, ast.Name) and node.target.id == "_module":
            return [e.value for e in node.iter.elts if isinstance(e, ast.Constant)]
    raise AssertionError("builder_tools no longer lists the modules it loads")


MODULES = sorted({*_registered(), "core.builder_tools", "core.native_builder.change_ops",
                  "core.native_builder.change_tools", "core.native_builder.change_apply",
                  "core.native_builder.build_plan"})


def test_the_list_is_read():
    assert "core.native_builder.build_save" in MODULES and "core.native_builder.change_apply" in MODULES


@pytest.mark.parametrize("module", MODULES)
def test_it_imports_first_and_alone(module):
    done = subprocess.run([sys.executable, "-c", f"import {module}"], cwd=BACKEND, env=dict(os.environ),
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr[-2000:]
