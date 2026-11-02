"""A change that applied is said as changed, for every op in the table (F1285, TRI1; CAP3's tier 1, prod-04).

prod-04 asked to change one knowledge-base note; the planner sent knowledge_edit_document, the person typed "approve",
the note was edited — and the builder said "Nothing in your workspace changed." The run's what-changed line
(`run_writes`) counts only `resource_saved` events, and only the product tools that wrap the builder's own (workflows,
agents, schedules, custom tools) yield one: a knowledge note, a memory item, the settings, a skill applied silently.

`apply_change` is the one door every reviewed write, create and delete goes through, so it says the write of an applied
op whose tool said nothing — and only then, so nothing is said twice. This walks the whole table: an op added later is
counted without anyone remembering to. Acts are not here: each is a card the person presses outside the run, which
never prints the run's what-changed line.
"""
from __future__ import annotations

import contextlib
from pathlib import Path
from types import SimpleNamespace

import pytest

import core.builder_tools  # noqa: F401 — loads the builder's modules in the product's order
from core.native_builder import change_apply as A, change_ops as C
from core.orchestration import run_writes

WRITES = [op for op in C.OPS.values() if op.is_write and op.tool]


@pytest.fixture
def apply(monkeypatch):
    """apply_change on one ready item, with the run, the review and the registry stood in: the target reads as it was
    before the call and as the op leaves it after."""
    from core.native_builder import review
    from core.product_tools import areas, registry

    run = SimpleNamespace(shared_state={}, session_id="s1")
    phase = {"done": False, "emits": False}

    @contextlib.asynccontextmanager
    async def writing_op(*args, **kwargs):
        yield None

    async def read(target, target_id):
        if not phase["done"]:
            return {"v": "old"}
        return None if phase["op"].kind == "delete" else {"v": "new"}

    async def nothing(*args, **kwargs):
        return None

    async def not_made(item):
        return None, False

    async def made_id(item, result):
        return "new1"

    monkeypatch.setattr(A, "_run", lambda: ("builder_t", run))
    monkeypatch.setattr(A, "read", read)
    monkeypatch.setattr(A, "reviewed_of", lambda checked: [])
    monkeypatch.setattr(A, "_caller", lambda: SimpleNamespace(user_id="u1"))
    monkeypatch.setattr(A, "_card", nothing)
    monkeypatch.setattr(A, "_made_since", not_made)
    monkeypatch.setattr(A, "_created_id", made_id)
    monkeypatch.setattr(A.one_writer, "writing_op", writing_op)
    monkeypatch.setattr(review, "approved_review", lambda state: {})
    monkeypatch.setattr(areas, "ensure_loaded", lambda: None)

    async def run_one(op, *, name="Opening hours", emits=False, lands=True):
        phase.update(op=op, done=False)
        events: list[dict] = []

        async def invoke(tool, caller, args, **kwargs):
            phase["done"] = lands
            if emits:
                events.append({"type": "resource_saved", "kind": "orchestration", "action": "updated",
                               "id": "id1", "name": name})
            return {"ok": True}

        monkeypatch.setattr(registry, "invoke", invoke)
        item = {"op": op.name, "status": "ready", "says": "",
                "target": {"kind": op.target, "id": None if op.kind == "create" else "id1", "name": name},
                "call": {"tool": op.tool, "args": {}}, "before": {"v": "old"},
                "expected": None if op.kind == "delete" else {"v": "new"}}
        monkeypatch.setattr(A, "checked_in", lambda state: {"ops": [item]})
        out = await A.apply_change({}, None, events)
        state: dict = {}
        for event in events:
            if event.get("type") == "resource_saved":
                run_writes.record(state, event)
        return out, events, state.get(run_writes.TEXT_KEY, run_writes.NOTHING_CHANGED)

    return run_one


def test_the_walk_has_every_kind_of_write():
    assert {op.kind for op in WRITES} == {"write", "create", "delete"}
    assert "knowledge_edit_document" in {op.name for op in WRITES} and "settings_set" in {op.name for op in WRITES}


@pytest.mark.parametrize("op", WRITES, ids=[op.name for op in WRITES])
async def test_every_applied_op_is_said_as_changed(apply, op):
    out, events, said = await apply(op)
    assert out["applied"] == [op.name] and out["failed"] == []
    saved = [e for e in events if e.get("type") == "resource_saved"]
    assert len(saved) == 1, saved
    assert saved[0]["action"] == {"create": "created", "delete": "deleted"}.get(op.kind, "updated")
    assert said != run_writes.NOTHING_CHANGED, said


async def test_prod_04s_note_reads_as_changed(apply):
    _, _, said = await apply(C.OPS["knowledge_edit_document"])
    assert said == 'Changed the document "Opening hours".'


async def test_the_settings_read_as_the_workspaces(apply):
    _, _, said = await apply(C.OPS["settings_set"], name="workspace")
    assert said == "Changed this workspace's settings."


async def test_a_tool_that_says_its_own_write_is_not_said_twice(apply):
    op = next(op for op in WRITES if op.target == "workflow" and op.kind == "write")
    _, events, said = await apply(op, name="Invoice reader", emits=True)
    assert [e.get("type") for e in events].count("resource_saved") == 1
    assert said == 'Changed the workflow "Invoice reader".'


async def test_an_op_that_did_not_take_says_so_and_no_write(apply):
    out, events, said = await apply(C.OPS["knowledge_edit_document"], lands=False)
    assert out["applied"] == [] and "Not done" in out["effect"]
    assert not [e for e in events if e.get("type") == "resource_saved"]
    assert said == run_writes.NOTHING_CHANGED, "nothing changed, and the summary step prints the effect after it"


def test_the_panels_banner_names_each_kind():
    banner = (Path(__file__).resolve().parents[3] / "frontend/src/lib/builderEvents.ts").read_text()
    assert "knowledge_base: 'Knowledge base'" in banner and "memory: 'Memory item'" in banner
    assert "if (saved.kind === 'settings') return 'Workspace settings updated';" in banner
