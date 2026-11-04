"""Every module of the builder imports on its own, first, in a fresh process (F1327; CI2's runs 41778021499 and
41782456943).

The builder's tools that live in their own modules registered through `core.builder_tools`, which they imported at
their top; `builder_tools` imports them all again at its end so that importing it registers every tool. So a process
whose first import was one of them ran `builder_tools`' end while that module was half-loaded, and a module there that
takes names from it at import failed: `change_apply` and `build_plan` from `change_tools`, and others along the same
path. It has been so since fix-forward 16, hidden behind whichever file imported `builder_tools` first. They register
through `core.builder_registry` now, which imports none of them, so any of them may be imported first.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[2]
#: Every module under core/native_builder, the package itself, and the builder's modules `builder_tools` loads.
MODULES = sorted(
    ["core.native_builder", "core.builder_tools", "core.builder_registry", "core.builder_connect", "core.builder_readers",
     "core.builder_help", "core.builder_guard", "core.builder_research", "core.builder_actions"]
    + [f"core.native_builder.{p.stem}" for p in (BACKEND / "core" / "native_builder").glob("*.py")
       if p.stem != "__init__"])


@pytest.mark.parametrize("module", MODULES)
def test_a_builder_module_imports_first_in_a_fresh_process(module):
    done = subprocess.run([sys.executable, "-c", f"import {module}"], cwd=BACKEND, env=dict(os.environ),
                          capture_output=True, text=True, timeout=180)
    assert done.returncode == 0, done.stderr[-2000:]
