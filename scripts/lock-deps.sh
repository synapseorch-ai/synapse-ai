#!/usr/bin/env bash
# Regenerate the backend dependency lockfiles.
#
# Why this exists: Synapse once declared `mcp` as a bare package name. A fresh
# install resolved the mcp 2.0.0 rewrite, every native tool server died at
# import, and the backend came up with no tools. Upper bounds in
# requirements*.txt stop the next *major* release; this lock stops everything
# else, including the transitive tree (onnxruntime, tokenizers, starlette,
# chromadb) which can break users just as easily.
#
# The locks are `--universal`: one file resolves correctly across Python
# 3.11-3.14 and linux/macOS/Windows, using environment markers. Installers and
# CI read the lock; requirements*.txt stays the human-edited spec.
#
# Usage:
#   ./scripts/lock-deps.sh          # regenerate all locks
#   ./scripts/lock-deps.sh --check  # fail if the locks are stale (used in CI)
#
# Regenerating is a deliberate, reviewable commit. Read the diff — an unexpected
# major version bump in there is exactly the signal this machinery exists to give.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_DIR="$REPO_ROOT/backend"

# The oldest interpreter we support. --universal resolves for this and upward.
MIN_PYTHON="3.11"

# The uv that makes and checks the locks. Another version writes the same pins
# with differently ordered markers, so --check would call an unchanged lock
# stale (F1170: CI's uv 0.13.6 against 0.11.8). CI's setup-uv pins this too
# (backend/tests/install/test_the_locks_are_made_by_one_uv.py keeps them equal).
UV_VERSION="0.11.8"

if ! command -v uv >/dev/null 2>&1; then
    echo "error: uv is required to regenerate the locks." >&2
    echo "  install: curl -LsSf https://astral.sh/uv/$UV_VERSION/install.sh | sh" >&2
    exit 1
fi
HAVE_UV="$(uv --version | awk '{print $2}')"
if [ "$HAVE_UV" != "$UV_VERSION" ]; then
    echo "error: the locks are made with uv $UV_VERSION; this is uv $HAVE_UV, which writes markers differently." >&2
    echo "  switch: uv self update $UV_VERSION   (or: curl -LsSf https://astral.sh/uv/$UV_VERSION/install.sh | sh)" >&2
    exit 1
fi

CHECK_MODE=0
if [ "${1:-}" = "--check" ]; then
    CHECK_MODE=1
fi

# Run from backend/ so uv records relative paths in the lock header — absolute
# paths would make every regeneration a machine-specific diff.
cd "$BACKEND_DIR"

# compile <output-lock> <input-spec>... -- [extra uv args]
compile() {
    local output="$1"; shift
    local inputs=()
    local extra=()
    local seen_sep=0

    for arg in "$@"; do
        if [ "$arg" = "--" ]; then seen_sep=1; continue; fi
        if [ "$seen_sep" -eq 1 ]; then extra+=("$arg"); else inputs+=("$arg"); fi
    done

    local destination="$output"
    if [ "$CHECK_MODE" -eq 1 ]; then
        destination="$(mktemp)"
        # Seed the temp file with the committed lock. `uv pip compile` reads its
        # output file and preserves pins that still satisfy the spec, so writing
        # to an EMPTY temp file resolved every package to its newest release
        # instead. That made --check assert "every dependency is at its latest
        # version right now" rather than "the lock is reproducible" — so it
        # failed on every PR the moment anything upstream published, and
        # regenerating could not fix it. Seeding makes check mode do exactly
        # what a normal run does, which is the thing we actually want to verify.
        cp "$output" "$destination"
    fi

    echo "==> $output"
    uv pip compile --universal --quiet \
        --python-version "$MIN_PYTHON" \
        "${inputs[@]}" \
        "${extra[@]}" \
        -o "$destination"

    if [ "$CHECK_MODE" -eq 1 ]; then
        # uv stamps the invocation into a header comment; compare content only.
        if ! diff <(grep -v '^#' "$output") <(grep -v '^#' "$destination") >/dev/null; then
            echo "error: $output is stale — run ./scripts/lock-deps.sh and commit the result." >&2
            diff <(grep -v '^#' "$output") <(grep -v '^#' "$destination") || true
            rm -f "$destination"
            exit 1
        fi
        rm -f "$destination"
    fi
}

# The default install: base + coding extras, which the CLI and setup.py always
# install together.
compile requirements.lock requirements.txt requirements-coding.txt

# The extras and the worker set, each constrained by every lock before it, so a
# shared transitive dependency is the same version in every lock an install may
# combine. Constrained by the base lock alone, the worker and telemetry locks
# each picked their own wrapt (2.3.0 and 2.5.0), and an install of both could
# not resolve (F1160). scripts/lock_agreement.py, run below, checks the result.
compile requirements-messaging.lock requirements-messaging.txt \
    -- --constraint requirements.lock

compile requirements-telemetry.lock requirements-telemetry.txt \
    -- --constraint requirements.lock --constraint requirements-messaging.lock

compile requirements-worker.lock requirements.txt requirements-worker.txt \
    -- --constraint requirements.lock --constraint requirements-messaging.lock \
    --constraint requirements-telemetry.lock

# What the published package installs: pyproject.toml's dependencies, pinned.
# The installer passes it to `uv tool install -c` (scripts/build_installer.py
# puts it in synapse-install.pyz), so an install resolves the way CI tested
# rather than to whatever PyPI has that day. Constrained by the locks above so a
# package both name is the same version in every install.
compile requirements-wheel.lock ../pyproject.toml \
    -- --constraint requirements.lock --constraint requirements-messaging.lock \
    --constraint requirements-telemetry.lock --constraint requirements-worker.lock

# Knowledge's parsers (Docling). Two differences from the other extras: torch
# resolves from PyTorch's CPU index (Knowledge runs no model on a GPU, and PyPI's
# Linux torch brings gigabytes of CUDA), and typer is left out of the constraints
# — docling-core caps it below the base lock's pick, and chromadb, its only base
# user, accepts either. Everything else is held to the locks above.
cat requirements.lock requirements-messaging.lock requirements-telemetry.lock \
    requirements-worker.lock requirements-wheel.lock | grep -v '^typer==' > .knowledge-constraints.txt
trap 'rm -f .knowledge-constraints.txt' EXIT
compile requirements-knowledge.lock requirements-knowledge.txt \
    -- --constraint .knowledge-constraints.txt --torch-backend cpu

# The same, with the knowledge and telemetry extras: the installer's pins when an
# install has [knowledge] (scripts/build_installer.py → constraints-knowledge.txt).
# The set above cannot take [knowledge] at all — it pins typer above
# docling-core's cap — so this one leaves typer to the knowledge lock, and takes
# torch from PyTorch's CPU index as that lock does.
compile requirements-wheel-knowledge.lock ../pyproject.toml \
    -- --extra knowledge --extra telemetry \
    --constraint .knowledge-constraints.txt --constraint requirements-knowledge.lock \
    --torch-backend cpu
rm -f .knowledge-constraints.txt

# Each lock reproducible is not enough: two that an install combines must agree.
python3 "$REPO_ROOT/scripts/lock_agreement.py" "$BACKEND_DIR"

if [ "$CHECK_MODE" -eq 1 ]; then
    echo "Locks are up to date."
else
    echo "Done. Review the diff before committing."
fi
