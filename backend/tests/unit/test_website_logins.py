"""Website logins: a site's session, kept so a run can use it without anyone typing a password again (BRW2, D330, F1079).

The person logs in once in the browser's pane; the component hands back Playwright's storage state (cookies and
localStorage), which is as good as the password for as long as it lasts. So it is kept encrypted (AES-GCM, a key held
outside the store), never in plain text on disk, for 30 days or until its last cookie expires (DESIGN §3.4), and a
login past that reads as none: the run then asks its owner to log in again. These are the two calls BRW1's client
makes (`keep`, `state_for`) and the ones Settings → Website logins makes (`list_logins`, `forget`).
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

from core import website_logins as wl
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
    ref = run(wl.keep(None, state=state, sites=["portal.saltmarsh.example"], owner="you"))
    assert run(wl.state_for(ref)) == state


def test_nothing_of_the_login_is_on_disk_in_plain_text(_isolated):
    run(wl.keep(None, state=_state(-1), sites=["portal.saltmarsh.example"], owner="you"))
    for f in _files(_isolated):
        assert SECRET.encode() not in f.read_bytes(), f"{f} holds the cookie in plain text"


def test_the_key_is_kept_outside_the_store_and_only_for_its_owner(_isolated):
    run(wl.keep(None, state=_state(-1), sites=["a.example"], owner="you"))
    key = wl.key_path()
    assert key.is_file()
    assert (_isolated / "blobs") not in key.parents, "the key sits in the store it protects"
    if os.name == "posix":
        assert stat.S_IMODE(key.stat().st_mode) == 0o600


def test_a_login_lasts_until_its_last_cookie_expires():
    now = time.time()
    ref = run(wl.keep(None, state=_state(now + 1 * DAY, now + 2 * DAY, -1), sites=["a.example"], owner="you"))
    [row] = run(wl.list_logins())
    assert row["id"] == ref
    assert abs(row["expires_at"] - (now + 2 * DAY)) < 60


def test_a_login_lasts_at_most_thirty_days():
    now = time.time()
    run(wl.keep(None, state=_state(now + 400 * DAY), sites=["a.example"], owner="you"))
    [row] = run(wl.list_logins())
    assert abs(row["expires_at"] - (now + 30 * DAY)) < 60


def test_session_cookies_alone_last_thirty_days():
    now = time.time()
    run(wl.keep(None, state=_state(-1, -1), sites=["a.example"], owner="you"))
    [row] = run(wl.list_logins())
    assert abs(row["expires_at"] - (now + 30 * DAY)) < 60


def test_an_expired_login_reads_as_none_and_is_gone(monkeypatch):
    now = time.time()
    ref = run(wl.keep(None, state=_state(now + DAY), sites=["a.example"], owner="you"))
    monkeypatch.setattr(wl.time, "time", lambda: now + 2 * DAY)
    assert run(wl.state_for(ref)) is None
    assert run(wl.list_logins()) == []


def test_logging_in_again_keeps_the_same_login():
    ref = run(wl.keep(None, state=_state(-1), sites=["a.example"], owner="you"))
    fresh = _state(time.time() + 3 * DAY)
    assert run(wl.keep(ref, state=fresh, sites=["a.example", "b.example"], owner="you")) == ref
    assert run(wl.state_for(ref)) == fresh
    [row] = run(wl.list_logins())
    assert row["sites"] == ["a.example", "b.example"]


def test_forget_removes_the_login():
    ref = run(wl.keep(None, state=_state(-1), sites=["a.example"], owner="you"))
    assert run(wl.forget(ref)) is True
    assert run(wl.state_for(ref)) is None
    assert run(wl.forget(ref)) is False


def test_a_tampered_login_reads_as_none(_isolated):
    ref = run(wl.keep(None, state=_state(-1), sites=["a.example"], owner="you"))
    [f] = [p for p in _files(_isolated / "blobs") if p.name.startswith(ref)]
    record = json.loads(f.read_text())
    record["owner"] = "someone else"  # the owner is bound to the ciphertext
    f.write_text(json.dumps(record))
    assert run(wl.state_for(ref)) is None


def test_a_login_read_with_another_key_reads_as_none(monkeypatch):
    ref = run(wl.keep(None, state=_state(-1), sites=["a.example"], owner="you"))
    monkeypatch.setenv(wl.KEY_ENV, "A" * 43 + "=")
    assert run(wl.state_for(ref)) is None


@pytest.mark.parametrize("ref", ["../../etc/passwd", "wl_../x", "", "WL_ABC", "wl_" + "a" * 200])
def test_a_ref_that_is_not_a_login_id_is_none(ref):
    assert run(wl.state_for(ref)) is None
    assert run(wl.forget(ref)) is False


def test_the_list_never_carries_the_session():
    run(wl.keep(None, state=_state(-1), sites=["a.example"], owner="you"))
    [row] = run(wl.list_logins())
    assert set(row) == {"id", "owner", "sites", "created_at", "expires_at"}
    assert SECRET not in json.dumps(row)


def test_settings_lists_and_forgets_logins():
    from core.routes.website_logins import router

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    ref = run(wl.keep(None, state=_state(-1), sites=["a.example"], owner="you"))
    listed = client.get("/api/website-logins").json()
    assert [r["id"] for r in listed["logins"]] == [ref]
    assert SECRET not in json.dumps(listed)
    assert client.delete(f"/api/website-logins/{ref}").status_code == 200
    assert client.get("/api/website-logins").json()["logins"] == []
    assert client.delete(f"/api/website-logins/{ref}").status_code == 404
