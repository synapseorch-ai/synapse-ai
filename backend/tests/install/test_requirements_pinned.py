"""
Enforce the dependency-pinning policy (runs in the gate).

Synapse shipped a release where a fresh install silently pulled mcp 2.0.0 — a
breaking SDK rewrite — because `mcp` was declared as a bare package name. Every
native tool server died at import and the product started with no tools. CI never
noticed: it installed the same unpinned requirements and its tests didn't touch
the MCP boot path.

The policy that prevents a repeat:

  * every direct dependency carries an upper bound, so a new major release can
    never arrive unannounced;
  * `backend/requirements.lock` pins the full transitive tree, so installers and
    CI resolve byte-identical environments.

These tests guard the policy itself. Without them the bounds erode one
convenience edit at a time — the previous rationale for bare names is still
visible in git history, and it read as a feature.
"""
from __future__ import annotations

import pathlib
import tomllib

import pytest
from packaging.requirements import Requirement

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_BACKEND = _REPO_ROOT / "backend"
_LOCKFILE = _BACKEND / "requirements.lock"

_REQUIREMENTS_FILES = [
    "requirements.txt",
    "requirements-coding.txt",
    "requirements-messaging.txt",
    "requirements-worker.txt",
    "requirements-knowledge.txt",
]

# Bounds that mean "a new major release cannot arrive unannounced".
_UPPER_BOUND_OPERATORS = ("<", "<=", "==", "===", "~=")


def _parse_requirements(path: pathlib.Path) -> list[Requirement]:
    """Parse a requirements file, skipping comments, blanks, and -r/-c includes."""
    requirements = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        requirements.append(Requirement(line))
    return requirements


def _has_upper_bound(requirement: Requirement) -> bool:
    return any(spec.operator in _UPPER_BOUND_OPERATORS for spec in requirement.specifier)


def _pyproject_dependencies() -> list[Requirement]:
    data = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return [Requirement(dep) for dep in data["project"]["dependencies"]]


@pytest.mark.parametrize("filename", _REQUIREMENTS_FILES)
def test_requirements_file_dependencies_have_upper_bounds(filename: str):
    """A bare name or floor-only spec means the next major release ships to users
    unannounced — exactly how mcp 2.0.0 broke every install."""
    path = _BACKEND / filename
    if not path.exists():
        pytest.skip(f"{filename} not present")

    unbounded = [str(req) for req in _parse_requirements(path) if not _has_upper_bound(req)]

    assert not unbounded, (
        f"backend/{filename} has dependencies with no upper bound: {unbounded}. "
        "Add a `<next-major` cap so a breaking release can't reach users silently."
    )


def test_pyproject_dependencies_have_upper_bounds():
    """`pip install synapse-orch-ai` resolves from pyproject.toml, not from
    requirements.txt — this surface needs the same protection."""
    unbounded = [str(req) for req in _pyproject_dependencies() if not _has_upper_bound(req)]

    assert not unbounded, (
        f"pyproject.toml [project.dependencies] has unbounded entries: {unbounded}. "
        "These govern the PyPI install path and must be capped too."
    )


def test_mcp_is_capped_below_v2_everywhere():
    """The load-bearing pin. mcp 2.0 removed the lowlevel Server decorator API and
    changed read_timeout_seconds to float; v1.x is upstream's supported holding
    position while the migration is scheduled separately."""
    sources: dict[str, list[Requirement]] = {
        "pyproject.toml": _pyproject_dependencies(),
        "backend/requirements.txt": _parse_requirements(_BACKEND / "requirements.txt"),
    }

    for source, requirements in sources.items():
        mcp_requirements = [req for req in requirements if req.name == "mcp"]
        assert mcp_requirements, f"{source} does not declare mcp at all"

        for requirement in mcp_requirements:
            assert requirement.specifier.contains("1.29.0"), (
                f"{source} declares {requirement}, which excludes mcp 1.29.0"
            )
            assert not requirement.specifier.contains("2.0.0"), (
                f"{source} declares {requirement}, which allows mcp 2.0.0. That release "
                "breaks every server in backend/tools/ and every ClientSession."
            )


def test_lockfile_exists_and_is_fully_pinned():
    """Ranges bound the direct deps; the lock is what makes an install
    reproducible, including the transitive tree (onnxruntime, tokenizers,
    starlette, chromadb) that can break users just as easily."""
    assert _LOCKFILE.exists(), (
        "backend/requirements.lock is missing. Regenerate it with scripts/lock-deps.sh."
    )

    unpinned = []
    for requirement in _parse_requirements(_LOCKFILE):
        operators = {spec.operator for spec in requirement.specifier}
        if operators != {"=="}:
            unpinned.append(str(requirement))

    assert not unpinned, f"backend/requirements.lock has non-exact pins: {unpinned}"


def test_lockfile_pins_mcp_to_v1():
    """The lock is what installers actually resolve, so it is the pin that
    reaches users."""
    mcp_pins = [req for req in _parse_requirements(_LOCKFILE) if req.name == "mcp"]

    assert mcp_pins, "backend/requirements.lock does not pin mcp"
    for requirement in mcp_pins:
        assert requirement.specifier.contains("1.29.0") or not requirement.specifier.contains("2.0.0"), (
            f"backend/requirements.lock pins {requirement}, which resolves to mcp 2.x"
        )


def test_lockfile_covers_the_declared_direct_dependencies():
    """A stale lock silently drops back to whatever pip resolves. Every direct
    dependency in requirements.txt must appear in the lock."""
    locked_names = {req.name.lower().replace("_", "-") for req in _parse_requirements(_LOCKFILE)}
    declared = _parse_requirements(_BACKEND / "requirements.txt")

    missing = sorted(
        req.name for req in declared if req.name.lower().replace("_", "-") not in locked_names
    )

    assert not missing, (
        f"backend/requirements.lock is missing declared dependencies: {missing}. "
        "Regenerate it with scripts/lock-deps.sh."
    )
