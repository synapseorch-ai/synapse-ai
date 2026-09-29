"""
Every backend module imports — on the oldest Python we support, not only the newest.

Python 3.14 evaluates annotations lazily (PEP 649); 3.11–3.13 evaluate them
when the `def` runs. A class with a method named `list` makes `list[dict]` in a
later annotation of that class a call on the method, which 3.14 never notices
and 3.12 refuses at import: `core/notifications.py` stopped the backend from
starting on every Python but 3.14 that way, and the whole suite — run on 3.14 —
stayed green. CI runs this file on 3.11 (install.yml, pip-install-with-deps).
"""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[2]


def _modules():
    for package in ("core", "services"):
        for path in sorted((BACKEND / package).rglob("*.py")):
            parts = list(path.relative_to(BACKEND).with_suffix("").parts)
            if parts[-1] == "__init__":
                parts = parts[:-1]
            yield ".".join(parts)


@pytest.mark.parametrize("module", list(_modules()))
def test_it_imports(module):
    try:
        importlib.import_module(module)
    except ModuleNotFoundError as exc:
        # An optional extra this environment does not have (arq is the
        # worker's). Our own modules missing is a real failure.
        if (exc.name or "").split(".")[0] in ("core", "services", "tools"):
            raise
        pytest.skip(f"needs {exc.name}")
