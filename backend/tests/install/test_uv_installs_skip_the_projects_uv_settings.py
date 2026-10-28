"""A uv command that installs or resolves packages, anywhere a user or contributor is told to run it, skips the repo's
own [tool.uv] settings (F1173).

pyproject.toml's [tool.uv] holds the repo root's uv.lock to the installer's locks (constraint-dependencies, PyTorch's
CPU index), and uv finds it walking up from wherever it runs, `uv pip` included. Measured 10-28 with uv 0.11.8, from
backend/: `uv pip compile requirements.lock` fails with it (typer 0.27.1 against the constraint's 0.26.8) and resolves
with --no-config; so does requirements-worker.lock; requirements-knowledge.lock resolves either way, to the same
versions. So every such command passes --no-config, except the two that must or cannot read it (ALLOWED).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]

#: Where a user or contributor is given a uv command, or the product runs one.
SCANNED = ("setup.sh", "setup.ps1", "start.sh", "start.ps1", "README.md", "CONTRIBUTING.md", "synapse/**/*.py",
           "scripts/*.sh", "scripts/*.py", "Dockerfile*", "docker-compose*.yml", ".github/workflows/*.yml",
           "docs/**/*.md", "docs/**/*.mdx", "backend/requirements*.txt", "backend/requirements*.lock")

#: A uv command that resolves packages: as text, or as a Python argument list.
#: Text names a command it can be run as, not one prose mentions: "`uv pip compile` reads…" closes its backtick (or
#: says "'s") right after the command's name.
_RESOLVES = re.compile(
    r"""\buv\s+(?:pip\s+(?:install|compile|sync)|sync|lock|add|tool\s+install|run\b[^\n`]*--with)(?![\w-]|`|'s)"""
    r"""|uv\b[\w.]*\s*,\s*["'](?:pip["']\s*,\s*["'](?:install|compile|sync)|sync|lock|add|tool["']\s*,\s*["']install)["']""")

#: (file, the command's own words) that may read [tool.uv], and why.
ALLOWED = {
    ("scripts/lock-deps.sh", "uv lock"): "makes uv.lock, which is what [tool.uv] is for",
    ("scripts/uv_constraints.py", "uv lock"): "says why uv lock reads the constraints it writes",
    ("scripts/lock-deps.sh", "uv tool install -c"): "says what the installer runs (flow.py, cwd = <home>/runtime)",
    ("synapse/installer/flow.py", "tool"): "its `uv tool install` runs with cwd = <home>/runtime, never in a checkout "
                                           "(F834)",
}


def offending(path: str, text: str) -> list[str]:
    """The lines of `text` that run a resolving uv command without --no-config and are not ALLOWED."""
    out = []
    for line in text.splitlines():
        if not _RESOLVES.search(line) or "--no-config" in line:
            continue
        if any(path == file and words in line for file, words in ALLOWED):
            continue
        out.append(line.strip())
    return out


def _files():
    seen = set()
    for pattern in SCANNED:
        for path in sorted(ROOT.glob(pattern)):
            if path.is_file() and path not in seen:
                seen.add(path)
                yield path


def test_the_scan_reads_the_places_that_matter():
    found = {p.relative_to(ROOT).as_posix() for p in _files()}
    assert {"scripts/lock-deps.sh", "synapse/installer/flow.py", "backend/requirements-knowledge.txt"} <= found


@pytest.mark.parametrize("path", [p.relative_to(ROOT).as_posix() for p in _files()])
def test_a_uv_command_that_resolves_skips_the_projects_settings(path):
    assert offending(path, (ROOT / path).read_text(encoding="utf-8", errors="replace")) == []


@pytest.mark.parametrize("line,flagged", [
    ("uv pip install -r requirements-knowledge.lock --torch-backend cpu", True),
    ("uv pip install --no-config -r requirements-knowledge.lock --torch-backend cpu", False),
    ("    uv pip compile --universal --quiet \\", True),
    ('cmd = [self.uv, "tool", "install", "--force"]', True),
    ("uv run --no-project --quiet --python 3.12 python installer.pyz", False),
    ("uv run --with pandas python x.py", True),
    ("uv tool uninstall synapse-orch-ai", False),
    ("uv sync", True),
    ("# The uv lock-deps.sh requires (UV_VERSION there)", False),
    ("`uv pip compile` reads its output file", False),
])
def test_the_scan_knows_a_resolving_command(line, flagged):
    assert bool(offending("elsewhere.sh", line)) is flagged
