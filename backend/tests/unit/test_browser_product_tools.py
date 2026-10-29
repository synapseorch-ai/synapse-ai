"""Recordings and kept sessions outside the app, as product tools (F1188; BRW-API, BRW1, D328).

An MCP client may list and read the workspace's recordings and the sessions the caller may use, and delete a
recording, through the registry's one door: a read is anyone's, a delete a builder's, previewed with a token, audited
as the app's DELETE is (`recording.deleted`). Each answer is an allowlist shared with cloud's public API: a recording's
steps as text and never their pictures, a session's site and dates and never what it holds; another person's session
they have not shared is not listed, nor their name.
"""
from __future__ import annotations

import base64
import json
import time

import pytest

from _fakes import seed as S
from core import site_sessions
from core.browser import kept, recordings
from core.product_tools import areas, confirm, hooks, registry
from core.product_tools.caller import Caller
from core.product_tools.registry import ToolRefusal, invoke
from core.product_tools.roles import Role
from core.storage import get_blob_store, set_blob_store
from core.storage.base import LocalBlobStore

COOKIE = "sess-77c1-a-cookie-no-answer-may-carry"
JPEG = b"\xff\xd8\xff\xe0" + b"picture-bytes-no-answer-may-carry" * 4
SHOT_REF = "r1-s2-shot"
ORDER = {"role": "textbox", "name": "Order number", "label": "Order number", "css": "#order-number-css-path"}


def _steps() -> list[dict]:
    return [{"id": "s1", "action": "open", "url": "https://portal.saltmarsh.example/orders", "after_shot": "r1-s1"},
            {"id": "s2", "action": "type", "target": ORDER, "value": {"input": "order number"},
             "after_shot": SHOT_REF},
            {"id": "s3", "action": "click", "target": {"role": "button", "name": "Find"}, "title": "Find the order"}]


def _state() -> dict:
    return {"cookies": [{"name": "sid", "value": COOKIE, "domain": "portal.saltmarsh.example", "path": "/",
                         "expires": time.time() + 86_400}],
            "origins": [{"origin": "https://portal.saltmarsh.example", "localStorage": [{"name": "t", "value": COOKIE}]}]}


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("SYNAPSE_HOME", str(tmp_path / "home"))
    monkeypatch.delenv(site_sessions.KEY_ENV, raising=False)
    was = get_blob_store()
    set_blob_store(LocalBlobStore(tmp_path / "blobs"))
    confirm.set_claim_store(None)
    areas.load()
    yield
    set_blob_store(was)
    hooks.set_audit(None)
    site_sessions.set_lister(None)
    from core.product_tools import browser
    browser.set_names(None)


@pytest.fixture
async def world():
    me = site_sessions.install_person()
    b64 = base64.b64encode(JPEG).decode()
    rec = await recordings.create("Find an order", _steps(), by=me, shots={"r1-s1": b64, SHOT_REF: b64})
    mine = await site_sessions.keep(None, state=_state(), site="portal.saltmarsh.example", owner=me)
    priyas = await site_sessions.keep(None, state=_state(), site="ledger.example", owner="priya")
    await S.seed_agents([S.make_agent(id="agent_web", name="Web", type="browser",
                                      browser={"recordings": [rec["id"]]})])
    await S.seed_orchestrations([S.make_orchestration(id="orch_w", name="Orders", steps=[
        {"id": "find", "name": "Find", "type": "browser", "recording_id": rec["id"], "next_step_id": None}])])
    return {"rec": rec["id"], "mine": mine, "priyas": priyas, "me": me}


@pytest.fixture
def audited():
    events: list[hooks.AuditEvent] = []

    async def record(event):
        events.append(event)

    hooks.set_audit(record)
    return events


def _caller(role=Role.BUILDER, scopes=None) -> Caller:
    return Caller(role=role, via="mcp", scopes=scopes)


class TestTheEntries:
    def test_what_each_is(self):
        got = {n: registry.get(n) for n in ("list_recordings", "get_recording", "list_browser_sessions",
                                            "delete_recording")}
        for name in ("list_recordings", "get_recording", "list_browser_sessions"):
            assert got[name].kind == "read" and got[name].scope == "read" and got[name].min_role == Role.VIEWER
            assert got[name].annotations()["readOnlyHint"] is True
        assert got["list_recordings"].paginated and got["list_browser_sessions"].paginated
        delete = got["delete_recording"]
        assert (delete.kind, delete.scope, delete.min_role, delete.irreversible) == ("act", "build", Role.BUILDER, True)
        assert delete.annotations()["destructiveHint"] is True and delete.action == "recording.deleted"
        for tool in got.values():
            assert not tool.app_only
            assert "in the app" in tool.description, tool.name  # what is not a tool, by design

    def test_a_viewer_and_an_approver_see_the_reads_and_not_the_delete(self):
        for role in (Role.VIEWER, Role.APPROVER):
            offered = {t.name for t in registry.for_caller(_caller(role))}
            assert {"list_recordings", "get_recording", "list_browser_sessions"} <= offered
            assert "delete_recording" not in offered

    def test_a_connection_without_build_is_not_offered_the_delete(self):
        assert "delete_recording" not in {t.name for t in registry.for_caller(_caller(scopes=frozenset({"read"})))}


class TestTheReads:
    async def test_the_list_says_what_each_recording_is(self, world):
        page = await invoke("list_recordings", _caller(Role.VIEWER), {})
        assert page["total"] == 1 and page["next_cursor"] is None
        assert page["items"] == [{
            "id": world["rec"], "name": "Find an order", "sites": ["saltmarsh.example"], "steps": 3,
            "inputs": ["order number"], "version": 1, "owner_workflow": None,
            "created_at": page["items"][0]["created_at"], "updated_at": page["items"][0]["updated_at"],
            "created_by_name": world["me"]}]

    async def test_one_recording_s_steps_are_text(self, world):
        doc = await invoke("get_recording", _caller(Role.VIEWER), {"recording_id": world["rec"]})
        assert doc["steps"] == [
            {"id": "s1", "n": 1, "action": "open", "line": "Open https://portal.saltmarsh.example/orders",
             "url": "https://portal.saltmarsh.example/orders"},
            {"id": "s2", "n": 2, "action": "type", "line": "Type {order number} into Order number",
             "url": None},
            {"id": "s3", "n": 3, "action": "click", "line": "Find the order", "url": None}]
        assert doc["pending_edits"] == 0 and doc["outputs"] == [] and doc["updated_by_name"] == world["me"]

    async def test_one_that_is_gone_says_so(self, world):
        with pytest.raises(ToolRefusal, match="That recording is gone."):
            await invoke("get_recording", _caller(), {"recording_id": "rec_00000000000000000000"})

    async def test_a_host_names_the_people_by_their_ids(self, world):
        from core.product_tools import browser

        async def names(ids):
            return {i: "Naveen" for i in ids}

        browser.set_names(names)
        doc = await invoke("get_recording", _caller(), {"recording_id": world["rec"]})
        assert doc["created_by_name"] == "Naveen" == doc["updated_by_name"]

    async def test_the_sessions_are_the_caller_s_own_and_nobody_else_s(self, world):
        page = await invoke("list_browser_sessions", _caller(Role.VIEWER), {})
        assert page["items"] == [{"id": world["mine"], "site": "saltmarsh.example", "owner": "you",
                                  "owner_name": world["me"], "expires_at": page["items"][0]["expires_at"],
                                  "last_used_at": None, "status": "active"}]
        assert "priya" not in json.dumps(page) and world["priyas"] not in json.dumps(page)

    async def test_a_host_lists_its_own_and_one_shared_shows_whose(self, world):
        seen = []

        async def lister(caller):
            seen.append(caller)
            return [{"id": "s-a", "site": "a.example", "owner": "u2", "owner_name": "Priya", "mine": False,
                     "usable": True, "shared_with": ["u1"], "expires_at": 1.0, "last_used_at": None, "expired": True,
                     "sealed": "never-out"},
                    {"id": "s-b", "site": "b.example", "owner": "u3", "owner_name": "Sam", "mine": False,
                     "usable": False, "shared_with": [], "expires_at": 2.0, "last_used_at": None, "expired": False}]

        site_sessions.set_lister(lister)
        caller = _caller()
        page = await invoke("list_browser_sessions", caller, {})
        assert seen == [caller]
        assert page["items"] == [{"id": "s-a", "site": "a.example", "owner": "shared", "owner_name": "Priya",
                                  "expires_at": 1.0, "last_used_at": None, "status": "expired"}]


class TestTheDelete:
    async def test_it_previews_what_uses_it_then_deletes_it_and_its_pictures(self, world, audited):
        args = {"recording_id": world["rec"]}
        first = await invoke("delete_recording", _caller(), args)
        assert first["status"] == "needs_confirmation"
        assert first["preview"]["will_delete"] == {"type": "recording", "id": world["rec"], "name": "Find an order",
                                                   "steps": 3}
        assert first["preview"]["used_by"] == {"workflows": [{"id": "orch_w", "name": "Orders", "step": "find"}],
                                               "agents": [{"id": "agent_web", "name": "Web"}]}
        assert await recordings.get(world["rec"]) and audited == []
        done = await invoke("delete_recording", _caller(), {**args, "confirm_token": first["confirm_token"]})
        assert done == {"deleted": world["rec"], "name": "Find an order"}
        with pytest.raises(recordings.NotFound):
            await recordings.get(world["rec"])
        assert kept.read_recording_shot(world["rec"], SHOT_REF) is None
        [event] = audited
        assert (event.action, event.target_type, event.target_id, event.confirmed_by) == (
            "recording.deleted", "recording", world["rec"], "token")

    @pytest.mark.parametrize("role", [Role.VIEWER, Role.APPROVER])
    async def test_below_builder_it_is_refused(self, world, role):
        with pytest.raises(ToolRefusal, match="needs the builder role"):
            await invoke("delete_recording", _caller(role), {"recording_id": world["rec"]})
        assert await recordings.get(world["rec"])

    async def test_one_that_is_gone_says_so_before_any_token(self, world):
        with pytest.raises(ToolRefusal, match="That recording is gone."):
            await invoke("delete_recording", _caller(), {"recording_id": "rec_00000000000000000000"})

    async def test_a_workflow_s_own_recordings_still_go_with_it(self, world):
        owned = await recordings.create("Made here", _steps(), by="you", owner_workflow="orch_gone")
        assert (await recordings.delete_owned("orch_gone"))["deleted"] == [owned["id"]]


class TestNothingHeldLeaves:
    async def test_no_answer_carries_a_cookie_a_picture_or_a_target(self, world):
        caller = _caller()
        answers = [await invoke("list_recordings", caller, {}),
                   await invoke("get_recording", caller, {"recording_id": world["rec"]}),
                   await invoke("list_browser_sessions", caller, {}),
                   await invoke("delete_recording", caller, {"recording_id": world["rec"]})]
        text = json.dumps(answers)
        for held in (COOKIE, "sealed", "nonce", "after_shot", SHOT_REF, "r1-s1", "/shots/",
                     base64.b64encode(JPEG).decode()[:16], "picture-bytes", "#order-number-css-path", "css",
                     "history", "shared_with"):
            assert held not in text, held
