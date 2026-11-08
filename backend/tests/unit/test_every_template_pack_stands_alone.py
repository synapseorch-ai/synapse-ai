"""Every shipped template pack imports on its own: nothing it names lives only in another pack (F790).

developer_pack's two workflows used `agent_jira_analyst` and `agent_slack_notifier`, which only
productivity_pack defined, and productivity_pack's Market Intelligence used `agent_web_researcher`,
which only starter_pack and simple_orch did. A pack imported alone warned that its steps used an
agent "which isn't in this file or on this install", and those steps failed at run time. In cloud
it was worse: an import gives every agent a new id, so the reference dangled even with the other
pack imported first.

Each check is over every pack, for every kind of reference the import route warns about
(`core/routes/import_export.py::_warnings`): a step's agent, an agent's delegates, an orchestrator
agent's workflow, a Workflow step's workflow.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"
PACKS = sorted(p.name for p in EXAMPLES.glob("*.bundle.json"))


def _load(name: str) -> dict[str, Any]:
    return json.loads((EXAMPLES / name).read_text())


def _steps(orch: dict[str, Any]) -> list[dict[str, Any]]:
    return [s for s in orch.get("steps") or [] if isinstance(s, dict)]


def test_the_packs_are_found() -> None:
    assert len(PACKS) >= 5


@pytest.mark.parametrize("name", PACKS)
def test_every_agent_a_pack_uses_is_in_the_pack(name: str) -> None:
    bundle = _load(name)
    agents = {a["id"] for a in bundle.get("agents") or []}
    used = {
        f"{o['id']}.{s['id']}": s["agent_id"]
        for o in bundle.get("orchestrations") or []
        for s in _steps(o)
        if s.get("agent_id")
    }
    used |= {
        f"{a['id']}.delegate": other
        for a in bundle.get("agents") or []
        for other in a.get("delegate_agent_ids") or []
    }
    missing = {where: agent for where, agent in used.items() if agent not in agents}
    assert not missing, f"{name} uses agents only another pack defines: {missing}"


@pytest.mark.parametrize("name", PACKS)
def test_every_workflow_a_pack_runs_is_in_the_pack(name: str) -> None:
    bundle = _load(name)
    orchs = {o["id"] for o in bundle.get("orchestrations") or []}
    runs = {
        f"{a['id']}.orchestration": a["orchestration_id"]
        for a in bundle.get("agents") or []
        if a.get("type") == "orchestrator" and a.get("orchestration_id")
    }
    runs |= {
        f"{o['id']}.{s['id']}": s["workflow_id"]
        for o in bundle.get("orchestrations") or []
        for s in _steps(o)
        if s.get("type") == "workflow" and s.get("workflow_id")
    }
    missing = {where: orch for where, orch in runs.items() if orch not in orchs}
    assert not missing, f"{name} runs workflows only another pack defines: {missing}"


def _as_imported(agent: dict[str, Any]) -> dict[str, Any]:
    """A key set to null is the key left out: the import stores both as null (simple_orch's
    `agent_web_researcher` spells out `"model": null`, starter_pack's leaves it out)."""
    return {k: v for k, v in agent.items() if v is not None}


def test_an_agent_two_packs_carry_is_the_same_in_both() -> None:
    """An import replaces an agent by id, so importing a second pack must not change the first's."""
    seen: dict[str, tuple[str, dict[str, Any]]] = {}
    differ = []
    for name in PACKS:
        for agent in map(_as_imported, _load(name).get("agents") or []):
            if agent["id"] in seen and seen[agent["id"]][1] != agent:
                differ.append(f"{agent['id']} ({seen[agent['id']][0]} vs {name})")
            seen.setdefault(agent["id"], (name, agent))
    assert not differ, f"defined differently by two packs: {differ}"


def test_the_index_counts_what_each_pack_holds() -> None:
    """The gallery card says what an import brings (F817: developer_pack said 2 apps, held 4)."""
    for entry in json.loads((EXAMPLES / "index.json").read_text()):
        bundle = _load(entry["file"])
        held = {
            "agent_count": len(bundle.get("agents") or []),
            "orchestration_count": len(bundle.get("orchestrations") or []),
            "mcp_count": len(bundle.get("mcp_servers") or []),
            "tool_count": len(bundle.get("custom_tools") or []),
        }
        assert {k: entry[k] for k in held} == held, entry["id"]
