"""Public run reads return only public state (F1205).

Every door that shows a run's shared state shows it as `events_public.public_state` does, without the keys the engine
keeps for itself. That list is named key by key on purpose (a person's own state key may start with an underscore),
so this walks the engine's code for every `_` key it writes into a run's state and fails when one is not stripped: a
key added later without its line in the list fails here.
"""
from __future__ import annotations

import json
import pathlib
import re

import pytest

from core.events_public import public_event, public_state

CORE = pathlib.Path(__file__).resolve().parents[2] / "core"

#: `X_KEY = "_name"` constants, and a state read or write by a literal `_name` (or an f-string's `_name_{…}`).
_CONSTANT = re.compile(r'^\s*([A-Z_]*KEY)\s*=\s*"(_[A-Za-z0-9_]+)"', re.M)
#: Any other upper-case constant holding a `_name` (`_ASKED_SENT = "_asked_sent_back"`), counted when the same file uses
#: it as a state key (F1400: such a key reached public reads, named neither `*KEY` nor written as a literal).
_NAMED = re.compile(r'^\s*(_?[A-Z][A-Z0-9_]*)\s*=\s*"(_[a-z][a-z0-9_]*)"', re.M)
_BY_NAME = re.compile(r'(?:shared_state|state)(?:\[|\.get\(|\.setdefault\(|\.pop\()\s*(?:[A-Za-z_][A-Za-z0-9_]*\.)?'
                      r'(_?[A-Z][A-Z0-9_]*)\b')
_IN_STATE = re.compile(r'(?:shared_state|state)(?:\[|\.get\(|\.setdefault\(|\.pop\()\s*f?"(_[a-z][a-z0-9_]*)')
_F_KEY = re.compile(r'f"(_[a-z][a-z0-9_]*_)\{')

#: `_` keys the walk finds that are not a run's state, and why.
NOT_RUN_STATE = {
    "__run__": "a key of `step_progress` (the run's active time, its error), which no public read carries",
    "_attachment": "a knowledge record's metadata (`chunker.ATTACHMENT_KEY`), not a run's",
    "_attachment_sha256": "a knowledge record's metadata, not a run's",
    "_pause": "a field of MCP's run view (`mcp_server.runs.PAUSE_KEY`), which its model view drops with every `_` field",
}


def engine_keys() -> dict[str, str]:
    """Every `_` key the engine's code writes into a run's state: key → where."""
    found: dict[str, str] = {}
    for path in CORE.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        where = str(path.relative_to(CORE))
        for m in _CONSTANT.finditer(text):
            found.setdefault(m.group(2), f"{where}:{m.group(1)}")
        used = {m.group(1) for m in _BY_NAME.finditer(text)}
        for m in _NAMED.finditer(text):
            if m.group(1) in used:
                found.setdefault(m.group(2), f"{where}:{m.group(1)}")
        for m in _IN_STATE.finditer(text):
            found.setdefault(m.group(1), where)
        if path.parent.name in ("orchestration", "browser", "native_builder"):
            for m in _F_KEY.finditer(text):
                found.setdefault(m.group(1) + "s1", where)
    return {k: v for k, v in found.items() if k not in NOT_RUN_STATE}


def test_the_walk_finds_the_keys_it_is_for():
    keys = engine_keys()
    for known in ("_browser_sessions", "_caller", "_model_override", "_run_writes", "_step_tasks",
                  "_step_progress_s1", "_exec_memory_s1", "_routing_decision_s1", "_asked_sent_back"):
        assert known in keys, f"the walk no longer finds {known}: fix the walk, not this list"


@pytest.mark.parametrize("key", sorted(engine_keys()))
def test_every_key_the_engine_writes_is_stripped(key):
    assert public_state({key: "held", "order": "A-1"}) == {"order": "A-1"}, (
        f"{key} ({engine_keys()[key]}) reaches a public read: name it in events_public.PRIVATE_STATE_PREFIXES")


def test_a_person_s_own_underscore_key_stays_theirs():
    assert public_state({"_my_note": "kept"}) == {"_my_note": "kept"}


def test_a_finished_run_s_event_carries_none_of_them():
    state = {**{k: "held" for k in engine_keys()}, "order": "A-1"}
    shown = public_event({"type": "orchestration_complete", "run_id": "r1", "status": "completed",
                          "final_state": state})
    assert shown["final_state"] == {"order": "A-1"}


async def test_no_door_that_reads_a_run_carries_one(client, api_key, seed_orchestration, monkeypatch):
    from core.orchestration.engine import OrchestrationEngine
    from core.orchestration.state import SharedState

    held = {k: "held" for k in engine_keys()}
    started = OrchestrationEngine.run

    def run(self, message, run_id=None, initial_state=None, **kw):
        # The engine's keys, as its own steps write them: a caller may set none (D126), so they go in here.
        return started(self, message, run_id, initial_state={**(initial_state or {}), **held}, **kw)

    monkeypatch.setattr(OrchestrationEngine, "run", run)
    orch = await seed_orchestration(entry_step_id="p", steps=[
        {"id": "p", "name": "Say", "type": "print", "print_content": "hello", "output_key": "out",
         "next_step_id": None}])
    resp = await client.post(f"/api/v1/orchestrations/{orch['id']}/run", headers=api_key["headers"],
                             json={"message": "go", "initial_state": {"order": "A-1"}})
    assert resp.status_code == 200, resp.text
    ran = resp.json()
    stored = (await SharedState.restore(ran["run_id"])).run.shared_state
    assert "_browser_sessions" in stored, "the keys reached the run's state, so each door below has them to strip"
    reads = {"v1 sync run": ran,
             "the app's run": (await client.get(f"/api/orchestrations/runs/{ran['run_id']}")).json(),
             "v1's run": (await client.get(f"/api/v1/orchestrations/runs/{ran['run_id']}",
                                           headers=api_key["headers"])).json()}
    for door, body in reads.items():
        assert body.get("shared_state", {}).get("order") == "A-1", f"{door}: {body}"
        leaked = sorted(k for k in held if k in json.dumps(body.get("shared_state")))
        assert leaked == [], f"{door} carries the engine's own keys: {leaked}"
