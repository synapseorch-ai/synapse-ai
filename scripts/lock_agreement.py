#!/usr/bin/env python3
"""The backend's lock files agree with each other: a package two locks both pin is pinned to the same version (F1160).

Each extra lock was resolved against requirements.lock alone, so a package the base does not have could land at one
version in one extra and another in the next: requirements-worker.lock pinned wrapt 2.3.0 and
requirements-telemetry.lock 2.5.0, and an install of both could not resolve. `lock-deps.sh` now resolves each lock
against the ones before it, and runs this after every regeneration and in `--check`.

Two sets are installed apart and checked apart:
  * the base set — requirements, -messaging, -telemetry, -worker and -wheel — installed together in any mix;
  * the Knowledge set — requirements-knowledge and -wheel-knowledge — an install of its own, which may differ from
    the base set only in `KNOWLEDGE_OWN` (docling-core caps typer below the base's pick; see lock-deps.sh).
Versions are compared, not markers: the same versions under different markers agree.

    python3 scripts/lock_agreement.py [backend dir]     # exit 1 and list each conflict
"""
from __future__ import annotations

import itertools
import re
import sys
from collections import defaultdict
from pathlib import Path

BASE_SET = ("requirements.lock", "requirements-messaging.lock", "requirements-telemetry.lock",
            "requirements-worker.lock", "requirements-wheel.lock")
KNOWLEDGE_SET = ("requirements-knowledge.lock", "requirements-wheel-knowledge.lock")
#: What the Knowledge set pins differently from the base set, on purpose.
KNOWLEDGE_OWN = frozenset({"typer"})

_PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?==([^\s;]+)")


def pins(path: Path) -> dict[str, frozenset[str]]:
    """Each package a lock pins, by normalised name, with every version it pins it at (one per marker)."""
    found: dict[str, set[str]] = defaultdict(set)
    for line in path.read_text(encoding="utf-8").splitlines():
        match = _PIN.match(line)
        if match:
            found[re.sub(r"[-_.]+", "-", match.group(1)).lower()].add(match.group(2))
    return {name: frozenset(versions) for name, versions in found.items()}


def conflicts(backend: Path) -> list[str]:
    """Every package two locks that may share an install pin at different versions, one line each."""
    locks = {name: pins(backend / name) for name in BASE_SET + KNOWLEDGE_SET if (backend / name).exists()}
    out = []
    for a, b in itertools.combinations(locks, 2):
        across = (a in KNOWLEDGE_SET) != (b in KNOWLEDGE_SET)
        for name in sorted(locks[a].keys() & locks[b].keys()):
            if across and name in KNOWLEDGE_OWN:
                continue
            if locks[a][name] != locks[b][name]:
                out.append(f"{name}: {a} pins {', '.join(sorted(locks[a][name]))}; "
                           f"{b} pins {', '.join(sorted(locks[b][name]))}")
    return out


def main() -> int:
    backend = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "backend"
    found = conflicts(backend)
    for line in found:
        print(f"error: {line}", file=sys.stderr)
    if found:
        print("The locks disagree: run ./scripts/lock-deps.sh, which resolves each lock against the ones before it.",
              file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
