"""Recordings kept to replay (BRW1, D328, F1142; DESIGN §v5.2): made from a stopped recording, changed by anyone who
can build with every change a version, a stale change refused, fixes kept pending until approved, and deleted with
their pictures. The store is the engine's `collections`, one item per recording."""
from __future__ import annotations

import base64

import pytest

from core.browser import client as browser_client
from core.browser import kept, recordings

ORDER = {"role": "textbox", "name": "Order number", "label": "Order number", "css": "#order", "field": True}
JPEG = base64.b64encode(b"\xff\xd8picture").decode()


def _steps():
    return [{"id": "s1", "action": "open", "url": "https://portal.saltmarsh.example/orders", "target": None,
             "value": None, "after_shot": "r1-s1"},
            {"id": "s2", "action": "type", "target": ORDER, "value": {"fixed": "A-1001"}, "after_shot": None},
            {"id": "s3", "action": "press", "key": "Enter", "target": ORDER, "value": None, "after_shot": None}]


async def test_a_stopped_recording_is_kept_with_its_pictures_and_lines():
    out = await recordings.from_stop({"steps": _steps(), "shots": {"r1-s1": JPEG}}, by="you", name="Find an order")
    doc = await recordings.get(out["recording_id"])
    assert out == {"recording_id": doc["id"], "steps": 3} and doc["version"] == 1 and doc["created_by"] == "you"
    assert doc["sites"] == ["saltmarsh.example"] and doc["name"] == "Find an order"
    assert [s["line"] for s in doc["steps"]] == ["Open https://portal.saltmarsh.example/orders",
                                                 "Type “A-1001” into Order number", "Press Enter"]
    assert kept.read_recording_shot(doc["id"], "r1-s1") == b"\xff\xd8picture"
    [row] = await recordings.list_recordings()
    assert row["id"] == doc["id"] and row["steps"] == 3 and row["pending_edits"] == 0


async def test_every_change_is_a_version_and_a_change_from_an_older_one_is_refused():
    doc = await recordings.create("T", _steps(), by="you")
    steps = doc["steps"]
    steps[1] = {**steps[1], "value": {"input": "order number"}}
    changed = await recordings.patch(doc["id"], base_version=1, by="sam", steps=steps,
                                     inputs=[{"name": "order number", "label": "Order number", "step_id": "s2"}])
    assert changed["version"] == 2 and changed["updated_by"] == "sam"
    assert changed["steps"][1]["line"] == "Type {order number} into Order number", "the sentence follows the step"
    with pytest.raises(recordings.Stale) as stale:
        await recordings.patch(doc["id"], base_version=1, by="you", name="old copy")
    assert stale.value.version == 2
    assert [v["version"] for v in await recordings.versions(doc["id"])] == [2, 1]


async def test_one_recordings_write_never_drops_another():
    a = await recordings.create("A", _steps(), by="you")
    b = await recordings.create("B", _steps(), by="you")
    await recordings.patch(a["id"], base_version=1, by="you", name="A2")
    assert {r["name"] for r in await recordings.list_recordings()} == {"A2", "B"}
    assert (await recordings.get(b["id"]))["name"] == "B"


async def test_a_fix_waits_to_be_approved_and_a_re_record_replaces_the_rest():
    doc = await recordings.create("T", _steps(), by="you")
    fix = [{"id": "s2b", "action": "type", "target": {**ORDER, "label": "Order no."}, "value": {"fixed": "A-1001"}}]
    edit = await recordings.add_edit(doc["id"], step_id="s2", steps=fix, by="agent", run_id="run9")
    pending = await recordings.get(doc["id"])
    assert pending["version"] == 1 and pending["steps"][1]["id"] == "s2", "nothing changes until it is approved"
    approved = await recordings.approve_edit(doc["id"], edit, by="you")
    assert [s["id"] for s in approved["steps"]] == ["s1", "s2b", "s3"] and approved["version"] == 2
    assert approved["pending_edits"] == []
    again = await recordings.from_stop({"steps": [{"id": "s9", "action": "click", "target": {"name": "Search"}}]},
                                       by="you", recording_id=doc["id"], from_step="s2b")
    approved = await recordings.approve_edit(doc["id"], again["pending_edit"], by="you")
    assert [s["id"] for s in approved["steps"]] == ["s1", "s9"], "a re-record replaces its step and those after"
    other = await recordings.add_edit(doc["id"], step_id="s1", steps=[], by="agent")
    assert (await recordings.discard_edit(doc["id"], other))["pending_edits"] == []
    with pytest.raises(recordings.NotFound):
        await recordings.discard_edit(doc["id"], other)


async def test_deleting_a_recording_deletes_its_pictures():
    doc = await recordings.create("T", _steps(), by="you", shots={"r1-s1": JPEG})
    assert await recordings.delete(doc["id"]) is True
    assert kept.read_recording_shot(doc["id"], "r1-s1") is None
    with pytest.raises(recordings.NotFound):
        await recordings.get(doc["id"])
    assert await recordings.delete(doc["id"]) is False


async def test_recording_a_task_from_start_to_kept(client, monkeypatch):
    opened, viewer_calls, closed = [], [], []

    class _Tools:
        async def open_record_session(self, rid, *, sessions, start_url=""):
            opened.append((rid, sessions, start_url))
            return "bs_rec"

        async def owner_of(self, sid):
            return f"record:{opened[-1][0]}" if sid == "bs_rec" else None

        async def viewer(self, sid, path, body=None):
            viewer_calls.append(path)
            return 200, {"steps": _steps(), "shots": {"r1-s1": JPEG}} if path == "/record/stop" else {"recording": True}

        async def close_owner(self, owner):
            closed.append(owner)

    monkeypatch.setattr(browser_client, "tools", _Tools())
    new = (await client.post("/api/recordings/new", json={"sessions": [], "start_url": "portal.saltmarsh.example"})).json()
    assert new["browser_session_id"] == "bs_rec" and new["recording_id"].startswith("rec_")
    assert opened[-1][2] == "https://portal.saltmarsh.example"
    assert (await client.post("/api/browser/bs_rec/record/start", json={"from_step": "s2"})).status_code == 501
    stopped = (await client.post("/api/browser/bs_rec/record/stop", json={"keep": True, "name": "Find"})).json()
    assert stopped == {"recording_id": new["recording_id"], "steps": 3} and closed == [f"record:{new['recording_id']}"]
    got = (await client.get(f"/api/recordings/{new['recording_id']}")).json()
    assert got["name"] == "Find" and "history" not in got
    shot = await client.get(f"/api/recordings/{new['recording_id']}/shots/r1-s1")
    assert shot.status_code == 200 and shot.content == b"\xff\xd8picture"
    stale = await client.patch(f"/api/recordings/{new['recording_id']}", json={"base_version": 9, "name": "x"})
    assert stale.status_code == 409 and stale.json()["version"] == 1
    assert (await client.patch(f"/api/recordings/{new['recording_id']}",
                               json={"base_version": 1, "name": "Find an order"})).json()["version"] == 2
    assert [v["version"] for v in (await client.get(f"/api/recordings/{new['recording_id']}/versions")).json()["versions"]] == [2, 1]
    assert (await client.delete(f"/api/recordings/{new['recording_id']}")).json() == {"deleted": True}
    assert (await client.get(f"/api/recordings/{new['recording_id']}")).status_code == 404


async def test_two_saves_from_one_version_cannot_both_pass():
    """F1142 follow-up (BRW3): a change is checked and written in one transaction, the recording locked, so of two saves
    made from version 1 at once exactly one passes. (Here SQLite and the in-process lock; cloud proves FOR UPDATE on
    Postgres across processes.)"""
    import asyncio
    doc = await recordings.create("T", _steps(), by="you")
    results = await asyncio.gather(recordings.patch(doc["id"], base_version=1, by="a", name="from a"),
                                   recordings.patch(doc["id"], base_version=1, by="b", name="from b"),
                                   return_exceptions=True)
    assert sorted(type(r).__name__ for r in results) == ["Stale", "dict"]
    assert (await recordings.get(doc["id"]))["version"] == 2


async def test_a_change_that_refuses_leaves_the_item_as_it_was():
    from core.store import collections
    await collections.put_item("t_items", "k", {"n": 1})

    def refuse(old):
        old["n"] = 99
        raise ValueError("no")

    with pytest.raises(ValueError):
        await collections.update_item("t_items", "k", refuse)
    assert await collections.get_item("t_items", "k") == {"n": 1}
    assert await collections.update_item("t_items", "k", lambda old: {**old, "n": 2}) == {"n": 2}
    assert await collections.update_item("t_items", "none", lambda old: old) is None
    await collections.put_item("t_items", "other", {"n": 5})
    assert await collections.delete_item("t_items", "k") is True and await collections.get_item("t_items", "other") == {"n": 5}
