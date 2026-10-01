"""The Knowledge contract is frozen (D153): any change to it shows up here as a diff.

A lane that needs a change files a contract-change request in the cloud repo's
`MEMORY/16-knowledge.md` §8, then regenerates the snapshots in the same commit:

    python backend/tests/fixtures/knowledge/make_fixtures.py --snapshots

Updating a snapshot is the declaration; the reviewer checks it against the request.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import textwrap
from pathlib import Path

from core.knowledge.testing.snapshot import api_schema, contract_snapshot

HERE = Path(__file__).resolve().parents[1] / "fixtures" / "knowledge"
BACKEND = Path(__file__).resolve().parents[2]


def _normal(value) -> object:
    return json.loads(json.dumps(value, sort_keys=True, default=str))


def _diff(stored: dict, current: dict) -> list[str]:
    out = []
    for key in sorted(set(stored) | set(current)):
        if stored.get(key) != current.get(key):
            out.append(key)
    return out


def test_the_contract_matches_its_snapshot():
    stored = json.loads((HERE / "contract_snapshot.json").read_text("utf-8"))
    current = _normal(contract_snapshot())
    assert current == stored, f"the Knowledge contract changed in: {_diff(stored, current)} — see this file's docstring"


def test_the_api_matches_its_snapshot():
    stored = json.loads((HERE / "openapi_knowledge.json").read_text("utf-8"))
    current = _normal(api_schema())
    assert current == stored, f"Knowledge API models changed: {_diff(stored, current)} — v1 only ever grows"


#: Modules a lane builds inside `core/knowledge/` — implementation, not contract
#: (CCR-2, 16 §8). A lane adds its own names here; `jobs.py` keeps only its
#: signature frozen (its docstring says so). Anything else in the directory is
#: contract and carries the header.
LANE_MODULES = {
    "__init__.py",
    # A1 — ingestion
    "bases.py", "documents.py", "sync.py", "report.py", "dispatch.py", "jobs.py", "ingest.py",
    # B1a — retrieval
    "chunker.py", "pipeline.py", "index_sql.py", "search.py", "trace_store.py", "snapshots.py",
    # B1b — agent tools and citations
    "tools.py", "citations.py",
    # B2a — embeddings, rerank, the re-embed backfill
    "profiles.py", "embedders.py", "rerankers.py", "backfill.py",
    # S1 — web sources: the crawl, its HTTP, sitemaps
    "web.py", "fetch.py", "sitemaps.py",
    # R1 — the Retrieve and Ingest workflow steps
    "workflow_steps.py",
    # B2b — budgets, cost previews and costs, presets
    "budgets.py", "costs.py",
}
FROZEN_HEADER = "# FROZEN CONTRACT (KB0, D153)"


def test_the_contract_modules_are_marked_frozen():
    for path in sorted((BACKEND / "core" / "knowledge").glob("*.py")):
        marked = path.read_text("utf-8").startswith(FROZEN_HEADER)
        if path.name in LANE_MODULES:
            assert not marked, f"{path.name} is a lane module; the frozen header would claim it is contract"
        else:
            assert marked, f"{path.name} is neither marked frozen nor a named lane module (CCR-2)"
    assert (BACKEND / "core" / "vault_events.py").read_text("utf-8").startswith(FROZEN_HEADER)


#: Modules cloud's API process imports: no numpy (cloud has none), no provider
#: clients, no engine loop, no routes.
PURE = [
    "core.knowledge.errors", "core.knowledge.ids", "core.knowledge.types", "core.knowledge.seams",
    "core.knowledge.parsed", "core.knowledge.chunks", "core.knowledge.status", "core.knowledge.filters",
    "core.knowledge.filters_sql", "core.knowledge.textsearch", "core.knowledge.vectors", "core.knowledge.trace",
    "core.knowledge.events", "core.knowledge.ledger", "core.knowledge.kb_settings", "core.knowledge.config",
    "core.knowledge.api_models", "core.knowledge.agent_contract", "core.knowledge.telemetry",
    "core.knowledge.jobs", "core.knowledge.pipeline_contract", "core.vault_events", "core.knowledge.testing.conformance",
    "core.knowledge.testing.reference_index", "core.knowledge.testing.fakes", "core.knowledge.testing.snapshot",
]
FORBIDDEN = ["numpy", "httpx", "core.llm_providers", "core.react_engine", "core.routes", "core.server",
             "docling", "chromadb"]


def test_the_contract_imports_nothing_heavy():
    script = textwrap.dedent(f"""
        import sys, importlib
        sys.path.insert(0, {str(BACKEND)!r})
        for name in {PURE!r}:
            importlib.import_module(name)
        loaded = sorted(m for m in sys.modules if any(m == f or m.startswith(f + ".") for f in {FORBIDDEN!r}))
        print(";".join(loaded))
    """)
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip() == "", f"the contract pulled in: {result.stdout.strip()}"


def test_only_testing_imports_the_testing_kit():
    offenders = []
    for path in (BACKEND / "core").rglob("*.py"):
        if "testing" in path.relative_to(BACKEND / "core").parts:
            continue
        if re.search(r"^\s*(from|import)\s+core\.knowledge\.testing", path.read_text("utf-8"), re.M):
            offenders.append(str(path.relative_to(BACKEND)))
    assert offenders == []
