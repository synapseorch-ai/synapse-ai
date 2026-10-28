#!/usr/bin/env python3
"""pyproject.toml's `[tool.uv] constraint-dependencies`, written from the installer's locks (F1173).

The repo root's uv.lock (`uv sync`, `uv run`) is resolved by `uv lock`, which reads no lock files: on its own it chose
its own versions, 28 of 138 shared packages apart from backend/requirements.lock. Its constraints are the installer's
pins instead: requirements-wheel-knowledge.lock (pyproject with the knowledge and telemetry extras), then the base
set's locks for anything it leaves out (redis, which the test extra's fakeredis brings, is pinned only there) —
except what Knowledge pins its own way (lock_agreement.KNOWLEDGE_OWN), since one uv.lock takes one version. The
base set agrees with itself (lock_agreement), so their order does not matter.

    python3 scripts/uv_constraints.py            # write them into pyproject.toml
    python3 scripts/uv_constraints.py --check    # exit 1 when pyproject.toml's are not these
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
PYPROJECT = ROOT / "pyproject.toml"
SOURCES = ("requirements-wheel-knowledge.lock", "requirements-wheel.lock", "requirements.lock",
           "requirements-worker.lock", "requirements-telemetry.lock", "requirements-messaging.lock")

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lock_agreement import KNOWLEDGE_OWN, _name  # noqa: E402

_PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?(==[^\s;]+)(\s*;\s*(.*))?$")
_BLOCK = re.compile(r"(?ms)^constraint-dependencies = \[\n.*?^\]\n")


def constraints() -> list[str]:
    """The pins, as PEP 508 strings: the Knowledge set's first, then the base's for names it lacks. A package pinned at
    one version is constrained to it everywhere (a constraint adds no package where none is wanted); its markers are
    kept only where the lock pins it at more than one (numpy by Python, torch by platform). A marker on a single pin
    only splits uv's resolution into forks it cannot show cover every environment, and then `uv lock --check` never
    passes."""
    out: list[str] = []
    seen: set[str] = set()
    for i, source in enumerate(SOURCES):
        found: dict[str, list[tuple[str, str]]] = {}
        for line in (BACKEND / source).read_text(encoding="utf-8").splitlines():
            match = _PIN.match(line.strip())
            if not match:
                continue
            name = _name(match.group(1))
            if name in seen or (i > 0 and name in KNOWLEDGE_OWN):
                continue
            found.setdefault(name, []).append((match.group(2), (match.group(4) or "").strip()))
        for name, pins in found.items():
            if len({version for version, _ in pins}) == 1:
                out.append(f"{name}{pins[0][0]}")
            else:
                out.extend(f"{name}{version} ; {marker}" if marker else f"{name}{version}" for version, marker in pins)
        seen |= set(found)
    return out


def block() -> str:
    lines = "".join(f'    "{c}",\n' for c in constraints())
    return f"constraint-dependencies = [\n{lines}]\n"


def main() -> int:
    text = PYPROJECT.read_text(encoding="utf-8")
    found = _BLOCK.search(text)
    if found is None:
        print("error: pyproject.toml has no [tool.uv] constraint-dependencies to keep", file=sys.stderr)
        return 1
    wanted = block()
    if found.group(0) == wanted:
        return 0
    if "--check" in sys.argv[1:]:
        print("error: pyproject.toml's constraint-dependencies are not the installer's locks — run "
              "./scripts/lock-deps.sh and commit the result.", file=sys.stderr)
        return 1
    PYPROJECT.write_text(text[:found.start()] + wanted + text[found.end():], encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
