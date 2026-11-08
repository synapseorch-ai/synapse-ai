"""Kept sessions: a site's signed-in session, kept so an agent or a run can use it without anyone typing a password
again (BRW1, D328, F1138; BRW2's website logins, F1079).

The person signs in once in the browser's pane; the component hands back Playwright's storage state (cookies and
localStorage) for that site, which is as good as the password for as long as it lasts. So it is kept encrypted
(AES-GCM, a key held outside the store), never in plain text on disk, for 30 days or until its last cookie expires,
for its owner only (DESIGN §v5.1). Past that its session is dropped and the record stays, marked expired, for signing
in again into it. These are the calls the browser's client makes (`keep`, `state_for`, `get`) and the ones the
Sessions library makes (`list_sessions`, `forget`).
"""
from __future__ import annotations

import asyncio
import json
import os
import stat
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import site_sessions as wl
from core.storage import get_blob_store, set_blob_store
from core.storage.base import LocalBlobStore

DAY = 86_400
SECRET = "sess-4f9a-very-secret-cookie-value"


def _state(*expires: float) -> dict:
    cookies = [{"name": f"c{i}", "value": SECRET, "domain": "portal.saltmarsh.example", "path": "/",
                "expires": e, "httpOnly": True, "secure": True, "sameSite": "Lax"} for i, e in enumerate(expires)]
    return {"cookies": cookies, "origins": [{"origin": "https://portal.saltmarsh.example",
                                             "localStorage": [{"name": "token", "value": SECRET}]}]}


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("SYNAPSE_HOME", str(home))
    monkeypatch.delenv(wl.KEY_ENV, raising=False)
    was = get_blob_store()
    set_blob_store(LocalBlobStore(tmp_path / "blobs"))
    yield tmp_path
    set_blob_store(was)


def run(coro):
    return asyncio.run(coro)


def _files(root) -> list:
    return [p for p in root.rglob("*") if p.is_file()]


def test_a_kept_login_reads_back_as_it_was_kept():
    state = _state(time.time() + 5 * DAY)
    ref = run(wl.keep(None, state=state, site="portal.saltmarsh.example", owner="you"))
    assert run(wl.state_for(ref)) == state


def test_nothing_of_the_login_is_on_disk_in_plain_text(_isolated):
    run(wl.keep(None, state=_state(-1), site="portal.saltmarsh.example", owner="you"))
    for f in _files(_isolated):
        assert SECRET.encode() not in f.read_bytes(), f"{f} holds the cookie in plain text"


def test_the_key_is_kept_outside_the_store_and_only_for_its_owner(_isolated):
    run(wl.keep(None, state=_state(-1), site="a.example", owner="you"))
    key = wl.key_path()
    assert key.is_file()
    assert (_isolated / "blobs") not in key.parents, "the key sits in the store it protects"
    if os.name == "posix":
        assert stat.S_IMODE(key.stat().st_mode) == 0o600


def test_a_login_lasts_until_its_last_cookie_expires():
    now = time.time()
    ref = run(wl.keep(None, state=_state(now + 1 * DAY, now + 2 * DAY, -1), site="a.example", owner="you"))
    [row] = run(wl.list_sessions())
    assert row["id"] == ref
    assert abs(row["expires_at"] - (now + 2 * DAY)) < 60


def test_a_login_lasts_at_most_thirty_days():
    now = time.time()
    run(wl.keep(None, state=_state(now + 400 * DAY), site="a.example", owner="you"))
    [row] = run(wl.list_sessions())
    assert abs(row["expires_at"] - (now + 30 * DAY)) < 60


def test_session_cookies_alone_last_thirty_days():
    now = time.time()
    run(wl.keep(None, state=_state(-1, -1), site="a.example", owner="you"))
    [row] = run(wl.list_sessions())
    assert abs(row["expires_at"] - (now + 30 * DAY)) < 60


def test_an_expired_session_drops_its_state_and_stays_for_signing_in_again(_isolated, monkeypatch):
    """DESIGN §v5.1: past its expiry it opens nothing, its sealed state is deleted, and the record stays (expired)
    so the owner signs in again into the same session and every pick of it keeps working; Remove deletes it."""
    now = time.time()
    ref = run(wl.keep(None, state=_state(now + DAY), site="a.example", owner="you"))
    monkeypatch.setattr(wl.time, "time", lambda: now + 2 * DAY)
    assert run(wl.state_for(ref)) is None
    [row] = run(wl.list_sessions())
    assert row["id"] == ref and row["expired"] is True
    [f] = [p for p in _files(_isolated / "blobs") if p.name == f"{ref}.json"]
    assert "sealed" not in json.loads(f.read_text()), "an expired session's state is not kept"


def test_signing_in_again_keeps_the_same_session():
    ref = run(wl.keep(None, state=_state(-1), site="a.example", owner="you"))
    fresh = _state(time.time() + 3 * DAY)
    assert run(wl.keep(ref, state=fresh, site="www.a.example", owner="you")) == ref
    assert run(wl.state_for(ref)) == fresh
    [row] = run(wl.list_sessions())
    assert row["site"] == "a.example" and row["expired"] is False


def test_forget_removes_the_login():
    ref = run(wl.keep(None, state=_state(-1), site="a.example", owner="you"))
    assert run(wl.forget(ref)) is True
    assert run(wl.state_for(ref)) is None
    assert run(wl.forget(ref)) is False


def test_a_tampered_login_reads_as_none(_isolated):
    ref = run(wl.keep(None, state=_state(-1), site="a.example", owner="you"))
    [f] = [p for p in _files(_isolated / "blobs") if p.name.startswith(ref)]
    record = json.loads(f.read_text())
    record["owner"] = "someone else"  # the owner is bound to the ciphertext
    f.write_text(json.dumps(record))
    assert run(wl.state_for(ref)) is None


@pytest.mark.parametrize("field, value", [("expires_at", 4_000_000_000), ("created_at", 0), ("site", "x.example"),
                                          ("owner", "someone")])
def test_a_login_edited_on_disk_reads_as_none(_isolated, field, value):
    """Its dates too: an `expires_at` moved on disk must not stretch the 30-day cap (BRW1's review)."""
    ref = run(wl.keep(None, state=_state(-1), site="a.example", owner="you"))
    [f] = [p for p in _files(_isolated / "blobs") if p.name.startswith(ref)]
    record = json.loads(f.read_text())
    record[field] = value
    f.write_text(json.dumps(record))
    assert run(wl.state_for(ref)) is None


def test_the_key_is_never_read_half_made(monkeypatch):
    """Several first writes at once agree on one whole key: none reads an empty or partial file (BRW1's review)."""
    import threading

    keys, errors = [], []
    start = threading.Barrier(8)

    def first():
        try:
            start.wait()
            keys.append(wl._key())
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=first) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(set(keys)) == 1 and len(keys[0]) == 32


def test_the_key_is_made_where_there_are_no_hard_links(monkeypatch):
    def no_links(*_a, **_k):
        raise OSError(1, "Operation not permitted")

    monkeypatch.setattr(wl.os, "link", no_links)
    ref = run(wl.keep(None, state=_state(-1), site="a.example", owner="you"))
    assert run(wl.state_for(ref)) == _state(-1)
    assert not [p for p in wl.key_path().parent.iterdir() if p.name.startswith(".")], "a temp key was left behind"


@pytest.mark.skipif(os.name != "posix", reason="file modes are POSIX")
def test_a_key_others_can_read_is_warned_about(caplog):
    run(wl.keep(None, state=_state(-1), site="a.example", owner="you"))
    os.chmod(wl.key_path(), 0o644)
    with caplog.at_level("WARNING", logger="core.site_sessions"):
        wl._key()
    assert any("readable" in r.getMessage() for r in caplog.records)
    assert not any(SECRET in r.getMessage() for r in caplog.records)


def test_a_login_read_with_another_key_reads_as_none(monkeypatch):
    ref = run(wl.keep(None, state=_state(-1), site="a.example", owner="you"))
    monkeypatch.setenv(wl.KEY_ENV, "A" * 43 + "=")
    assert run(wl.state_for(ref)) is None


@pytest.mark.parametrize("ref", ["../../etc/passwd", "wl_../x", "", "WL_ABC", "wl_" + "a" * 200])
def test_a_ref_that_is_not_a_login_id_is_none(ref):
    assert run(wl.state_for(ref)) is None
    assert run(wl.forget(ref)) is False


def test_the_list_never_carries_the_session():
    run(wl.keep(None, state=_state(-1), site="a.example", owner="you"))
    [row] = run(wl.list_sessions())
    assert set(row) == {"id", "owner", "site", "created_at", "last_used_at", "expires_at", "expired"}
    assert SECRET not in json.dumps(row)


def test_the_old_logins_screen_still_lists_and_forgets_for_one_release():
    from core.routes.site_sessions import router

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    ref = run(wl.keep(None, state=_state(-1), site="a.example", owner="you"))
    listed = client.get("/api/website-logins").json()
    assert [r["id"] for r in listed["logins"]] == [ref]
    assert SECRET not in json.dumps(listed)
    assert client.delete(f"/api/website-logins/{ref}").status_code == 200
    assert client.get("/api/website-logins").json()["logins"] == []
    assert client.delete(f"/api/website-logins/{ref}").status_code == 404



def test_a_session_kept_before_the_rename_still_opens_and_is_kept_the_new_way(_isolated):
    """F1079's records bound `sites` (a list) to their ciphertext; they read as their first host's site, and keeping
    one again writes it with `site`, under the same id."""
    import base64
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    ref, now = "wl_" + "a" * 24, time.time()
    record = {"id": ref, "owner": "you", "sites": ["portal.saltmarsh.example"], "created_at": now,
              "expires_at": now + DAY}
    nonce = os.urandom(12)
    aad = json.dumps({k: record[k] for k in ("id", "owner", "sites", "created_at", "expires_at")},
                     sort_keys=True).encode()
    sealed = AESGCM(wl._key()).encrypt(nonce, json.dumps(_state(-1)).encode(), aad)
    (wl._dir() / f"{ref}.json").write_text(json.dumps({**record, "nonce": base64.b64encode(nonce).decode(),
                                                       "sealed": base64.b64encode(sealed).decode()}))
    assert run(wl.state_for(ref)) == _state(-1)
    [row] = run(wl.list_sessions())
    assert row["site"] == "saltmarsh.example"
    assert run(wl.keep(ref, state=_state(-1), site="saltmarsh.example", owner="you")) == ref
    assert "site" in json.loads((wl._dir() / f"{ref}.json").read_text())
    assert run(wl.state_for(ref)) == _state(-1)


def test_a_session_opens_only_for_its_owner_and_says_when_it_was_used():
    ref = run(wl.keep(None, state=_state(-1), site="a.example", owner="priya"))
    assert run(wl.state_for(ref, for_user="sam")) is None
    assert run(wl.get(ref))["last_used_at"] is None
    assert run(wl.state_for(ref, for_user="priya")) == _state(-1)
    assert run(wl.get(ref))["last_used_at"] is not None
    assert ref.startswith("ss_") and run(wl.get("ss_" + "0" * 24)) is None


def test_the_library_lists_keeps_and_removes_sessions(monkeypatch):
    from core.routes import site_sessions as routes
    from core.browser import client as browser_client

    async def agents():
        return [{"id": "a1", "browser": {"sessions": []}}]

    opened, starts = [], []

    class _Tools:
        async def open_login_session(self, sites, *, start_url=""):
            opened.append(sites)
            starts.append(start_url)
            return "bs1"

    async def orchs():
        return []

    monkeypatch.setattr("core.store.resources.load_agents", agents)
    monkeypatch.setattr("core.store.resources.load_orchestrations", orchs)
    monkeypatch.setattr(browser_client, "tools", _Tools())
    app = FastAPI()
    app.include_router(routes.router)
    client = TestClient(app)
    ref = run(wl.keep(None, state=_state(-1), site="a.example", owner="you"))
    [row] = client.get("/api/site-sessions").json()["sessions"]
    assert row["id"] == ref and row["mine"] is True and row["shared_with"] == [] and row["owner_name"] == "you"
    assert row["uses"] == {"agents": 0, "chats": 0, "workflows": 0} and SECRET not in json.dumps(row)
    assert client.post("/api/site-sessions/new", json={"site": "https://portal.saltmarsh.example/login"}).json() \
        == {"browser_session_id": "bs1"} and opened == [["saltmarsh.example"]]
    assert starts == ["https://portal.saltmarsh.example"], "it opens on the host typed (F1425)"
    assert client.post("/api/site-sessions/new", json={"site": "nope"}).status_code == 422
    assert client.patch(f"/api/site-sessions/{ref}", json={"shared_with": "workspace"}).status_code == 404
    assert client.delete(f"/api/site-sessions/{ref}").status_code == 200
    assert client.delete(f"/api/site-sessions/{ref}").status_code == 404


@pytest.mark.parametrize("field, value", [("owner", "someone"), ("sites", ["evil.example"]),
                                          ("expires_at", 4_000_000_000)])
def test_a_session_kept_before_the_rename_edited_on_disk_opens_nothing(_isolated, field, value):
    """An old record's sealed fields bind as they did: an edited owner, hosts or expiry reads as no session."""
    import base64
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    ref, now = "wl_" + "b" * 24, time.time()
    record = {"id": ref, "owner": "you", "sites": ["portal.saltmarsh.example"], "created_at": now,
              "expires_at": now + DAY}
    nonce = os.urandom(12)
    aad = json.dumps({k: record[k] for k in ("id", "owner", "sites", "created_at", "expires_at")},
                     sort_keys=True).encode()
    sealed = AESGCM(wl._key()).encrypt(nonce, json.dumps(_state(-1)).encode(), aad)
    stored = {**record, "nonce": base64.b64encode(nonce).decode(), "sealed": base64.b64encode(sealed).decode()}
    (wl._dir() / f"{ref}.json").write_text(json.dumps({**stored, field: value}))
    assert run(wl.state_for(ref)) is None


def test_someone_elses_unshared_session_is_shown_never_picked_and_never_loaded(monkeypatch):
    """F1167 (BRW2's shots): the pickers offered Priya's unshared session. Every session is listed, `usable` only for
    the person's own; picking someone else's is refused (flat 422), and at the browser's start nothing of theirs loads."""
    from core.browser import client as browser_client
    from core.routes import site_sessions as routes

    async def none():
        return []

    monkeypatch.setattr("core.store.resources.load_agents", none)
    monkeypatch.setattr("core.store.resources.load_orchestrations", none)
    monkeypatch.setattr(browser_client, "_login_store", None)
    mine = run(wl.keep(None, state=_state(-1), site="a.example", owner="you"))
    hers = run(wl.keep(None, state=_state(-1), site="b.example", owner="priya"))
    app = FastAPI()
    app.include_router(routes.router)
    client = TestClient(app)
    rows = {r["id"]: r for r in client.get("/api/site-sessions").json()["sessions"]}
    assert rows[mine]["usable"] is True and rows[hers]["usable"] is False and rows[hers]["mine"] is False
    refused = client.put("/api/chat/c1/browser-sessions", json={"ids": [mine, hers]})
    assert refused.status_code == 422 and refused.json() == {
        "detail": "That session for b.example is priya's and isn't shared with you.", "site": "b.example",
        "ids": [hers]}
    assert client.put("/api/chat/c1/browser-sessions", json={"ids": [mine]}).json() == {"ids": [mine]}
    assert run(wl.state_for(hers)) is None, "this install's person never opens someone else's"
    state, expired, loaded, skipped = run(browser_client._sessions_state([mine, hers]))
    assert [l["id"] for l in loaded] == [mine] and expired is None
    assert skipped == [{"site": "b.example", "reason": "not_shared", "owner_name": "priya"}]
    assert len(state["cookies"]) == 1
