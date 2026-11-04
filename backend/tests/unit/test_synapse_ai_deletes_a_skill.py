"""Synapse AI deletes a skill as a reviewed change, and undoes it; installing one stays the Skills screen's (F1266,
REACH2; D340's reach row "skills: update; delete; install from the registry"; manager 79, 11-03).

The delete is the Skills screen's own code (`core.skills.delete_from_library`), which its route calls too, so both say
the same thing: a library skill only, a disk skill being its folder's. The review names the agents that select it
(they keep the name and load nothing until it is back), and "undo that" writes it back to the library as it was.

Installing from the Skills registry runs `npx skills add` and downloads instructions agents then follow as written:
its route is "UI only, never a tool an agent can call", and cloud has no installer. So there is no install op or card;
the census says it is done on the Skills screen, and the planner's `not_here` points there (79's ruling (b)).
"""
from __future__ import annotations

import json

import pytest

from core import builder_access, skills
from core.builder_tools import execute_builder_tool_with_events
from core.models_orchestration import OrchestrationRun
from core.orchestration.engine import live
from core.orchestration.state import SharedState
from core.product_tools import confirm, hooks
from core.product_tools.roles import Role


class _Server:
    agent_sessions: dict = {}
    tool_router: dict = {}


@pytest.fixture(autouse=True)
def fresh():
    confirm.set_claim_store(None)
    builder_access.forget_runs()

    async def owner(user_id):
        return Role.OWNER

    hooks.set_role_source(owner)
    yield
    hooks.set_role_source(None)
    builder_access.forget_runs()


_N = {"n": 0}


async def _call(tool, args, run):
    with live(run):
        raw, _ = await execute_builder_tool_with_events(tool, args, _Server(), run_id=run.run_id)
    return json.loads(raw)


async def _change(draft):
    """One whole change: check, review approved, apply. Returns (run, check, apply)."""
    _N["n"] += 1
    run = OrchestrationRun(run_id=f"builder_skill_{_N['n']}", orchestration_id="orch_native_builder",
                           shared_state={builder_access.CALLER_KEY: {"user_id": "u1"}})
    await SharedState(run).checkpoint()
    checked = await _call("check_change", {"draft": {"finish": "submit_change", **draft}}, run)
    if checked["outcome"] != "review":
        return run, checked, None
    run.shared_state["change_checked"] = json.dumps(checked)
    run.shared_state["manage_review"] = {"feedback": "approve"}
    await _call("approve_change", {}, run)
    return run, checked, await _call("apply_change", {}, run)


async def _skill(name="house-style", description="How we write", body="Short sentences. No jargon."):
    library = await skills.load_library()
    library.append({"id": name, "name": name, "description": description, "body": body})
    await skills.save_library(library)


async def test_a_skill_is_deleted_through_its_review_and_comes_back_on_undo(client):
    from core.store.resources import save_agent
    from tests._fakes.seed import make_agent

    await _skill()
    await save_agent(make_agent(name="Writer", skills=["house-style"]))
    first, checked, done = await _change({"ops": [{"op": "delete_skill",
                                                   "target": {"kind": "skill", "name": "house-style"}}]})
    assert 'agent "Writer"' in checked["review"] and done["applied"] == ["delete_skill"], (checked, done)
    assert await skills.get_library_skill("house-style") is None

    _, checked, undone = await _change({"undo_of": first.run_id})
    assert undone["applied"] == ["restore_skill"], undone
    back = await skills.get_library_skill("house-style")
    assert back == {"name": "house-style", "description": "How we write", "body": "Short sentences. No jargon.",
                    "source": "library"}


async def test_a_disk_skill_is_its_folder_s(client, monkeypatch):
    monkeypatch.setattr(skills, "scan_disk_skills", lambda: [{"name": "pdf", "description": "PDFs", "source": "disk"}])
    _, checked, done = await _change({"ops": [{"op": "delete_skill", "target": {"kind": "skill", "name": "pdf"}}]})
    assert done is None and "read from a folder on disk" in json.dumps(checked)
    with pytest.raises(skills.NotInLibrary, match="disk skills are read-only"):
        await skills.delete_from_library("pdf")


async def test_the_screen_s_route_says_what_it_said(client):
    """One place (F1189): the route calls `delete_from_library` and answers as before."""
    await _skill(name="tone")
    assert (await client.delete("/api/skills/tone")).json() == {"status": "success"}
    gone = await client.delete("/api/skills/tone")
    assert gone.status_code == 404 and gone.json()["detail"] == skills.NOT_IN_LIBRARY


def test_the_delete_is_the_app_s_own_and_a_builder_s():
    from core.product_tools import areas, registry

    areas.ensure_loaded()
    tool = registry.get("delete_skill")
    assert tool.app_only and tool.irreversible and tool.min_role == Role.BUILDER and tool.kind == "act"


def test_installing_is_the_skills_screen_s_never_a_card():
    """79's ruling (b): no install op, act or product tool; `not_here` names the Skills screen."""
    from core import feature_census
    from core.builder_actions import ACTIONS
    from core.native_builder import change_ops as C
    from core.product_tools import areas, registry

    areas.ensure_loaded()
    assert not [n for n in (*C.OPS, *ACTIONS) if "install" in n]
    assert registry.get("install_skill") is None
    row = next(s for s in feature_census.CENSUS if s.id == "skills")
    assert "delete_skill" in row.tools
    assert {"what": row.cant, "open": feature_census.link(row), "screen": "skills"} in feature_census.not_here()
    assert "/skills" in feature_census.link(row)
    assert "installing a skill" in row.cant
