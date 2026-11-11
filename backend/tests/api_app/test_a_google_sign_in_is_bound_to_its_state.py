"""A Google sign-in is kept only from the callback of a sign-in started here (F1535, BRW5; eb 11-11).

`/auth/callback` ignored `state`, so nothing tied a callback to a sign-in this install started. It exchanged any
`code` with the install's own Google client and kept the tokens as the install's Google connection, so its agents'
Gmail, Drive and Calendar tools would act on whoever's account that was (login CSRF). With no sign-in waiting it fell
back to a fresh flow with no PKCE verifier, so a code minted without a challenge was redeemed; one process-wide slot
held the waiting sign-in, which anyone could replace or use up; and an error's text went into the redirect unencoded.
`/auth/*` is reached without a session, through the frontend (its proxy lets `/auth/` past the login gate) and from a
person's own browser by any page that links there.

Now `/auth/login` gives each sign-in its own random `state`, kept with its flow for 10 minutes; only the callback that
carries it, compared in constant time, may use it, once; without a sign-in waiting nothing is redeemed; a refusal
says why and keeps nothing; the reason is encoded; and starting a sign-in takes a session when the install has a gate
(login on, or its access link), so a stranger can neither start one nor push out the person's.
"""
from __future__ import annotations

import json
from typing import ClassVar
from urllib.parse import parse_qs, urlsplit

import pytest

from core import internal_auth
from services import google


class _Creds:
    def __init__(self, code: str):
        self.token = f"tok-{code}"

    def to_json(self) -> str:
        return json.dumps({"token": self.token, "refresh_token": "r", "client_id": "cid"})


class _Flow:
    """google_auth_oauthlib's Flow, as far as the sign-in uses it."""

    made: ClassVar[list[_Flow]] = []

    def __init__(self):
        self.state = None
        self.credentials = None

    @classmethod
    def from_client_config(cls, client_config, scopes, redirect_uri=None, state=None, **_kw):
        flow = cls()
        flow.state = state
        cls.made.append(flow)
        return flow

    def authorization_url(self, **kwargs):
        # As OAuth2Session does: the state given, else a fresh one of its own.
        self.state = kwargs.get("state") or self.state or f"own-{len(self.made)}"
        return f"https://accounts.example/o/oauth2/auth?client_id=cid&state={self.state}", self.state

    def fetch_token(self, code=None, **_kw):
        if code == "boom":
            raise ValueError("bad code & more#here")
        self.credentials = _Creds(code)


@pytest.fixture
def kept(monkeypatch):
    """The Google tokens the install keeps, and a Google that answers as above."""
    saved: list[dict] = []
    _Flow.made = []

    async def client_config():
        return {"web": {"client_id": "cid", "client_secret": "s", "auth_uri": "https://accounts.example/o/oauth2/auth",
                        "token_uri": "https://accounts.example/token"}}

    async def save_token(token_data):
        saved.append(token_data)

    async def nothing():
        return None

    def no_userinfo(*_a, **_kw):
        raise OSError("no network here")

    monkeypatch.setattr(google, "Flow", _Flow)
    monkeypatch.setattr(google, "load_client_config", client_config)
    monkeypatch.setattr(google, "save_token", save_token)
    monkeypatch.setattr(google, "materialise_mcp_dir", nothing)
    monkeypatch.setattr("urllib.request.urlopen", no_userinfo)
    monkeypatch.setattr(google, "_pending_flow", None, raising=False)
    monkeypatch.setattr(google, "_pending", {}, raising=False)
    return saved


async def _sign_in(client, headers=None) -> str:
    resp = await client.get("/auth/login", headers=headers or {})
    assert resp.status_code in (302, 307), resp.text
    location = resp.headers["location"]
    assert location.startswith("https://accounts.example/"), location
    return parse_qs(urlsplit(location).query)["state"][0]


def _back(resp) -> dict[str, str]:
    """What the callback sent the person back to the app with."""
    assert resp.status_code in (302, 307), resp.text
    return {k: v[0] for k, v in parse_qs(urlsplit(resp.headers["location"]).query).items()}


async def test_the_callback_of_a_sign_in_started_here_still_connects(client, kept):
    state = await _sign_in(client)
    back = _back(await client.get("/auth/callback", params={"code": "c1", "state": state}))
    assert back == {"google_auth": "success"}
    assert [t["token"] for t in kept] == ["tok-c1"]


async def test_no_state_or_a_wrong_one_is_refused_keeps_nothing_and_uses_up_nothing(client, kept):
    state = await _sign_in(client)
    for params in ({"code": "c1"}, {"code": "c1", "state": "not-" + state}):
        back = _back(await client.get("/auth/callback", params=params))
        assert back["google_auth"] == "error" and "did not start here" in back["reason"], back
    assert kept == []
    back = _back(await client.get("/auth/callback", params={"code": "c2", "state": state}))
    assert back == {"google_auth": "success"} and [t["token"] for t in kept] == ["tok-c2"]


async def test_a_state_is_used_once(client, kept):
    state = await _sign_in(client)
    assert _back(await client.get("/auth/callback", params={"code": "c1", "state": state}))["google_auth"] == "success"
    again = _back(await client.get("/auth/callback", params={"code": "c2", "state": state}))
    assert again["google_auth"] == "error"
    assert [t["token"] for t in kept] == ["tok-c1"]


async def test_with_no_sign_in_waiting_nothing_is_redeemed(client, kept):
    back = _back(await client.get("/auth/callback", params={"code": "c1", "state": "anything"}))
    assert back["google_auth"] == "error"
    assert kept == []
    assert all(flow.credentials is None for flow in _Flow.made), "a code was redeemed with no sign-in waiting"


async def test_a_sign_in_waits_ten_minutes_at_most(client, kept, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(google, "_now", lambda: clock[0], raising=False)
    state = await _sign_in(client)
    clock[0] += 601
    back = _back(await client.get("/auth/callback", params={"code": "c1", "state": state}))
    assert back["google_auth"] == "error" and kept == []


async def test_the_reason_is_encoded(client, kept):
    state = await _sign_in(client)
    resp = await client.get("/auth/callback", params={"code": "boom", "state": state})
    assert _back(resp) == {"google_auth": "error", "reason": "bad code & more#here"}
    assert "&more" not in resp.headers["location"] and "#" not in resp.headers["location"]


async def test_a_stranger_can_neither_start_a_sign_in_nor_push_out_the_persons(client, kept, monkeypatch):
    monkeypatch.setattr(internal_auth, "_login_required", lambda: True)
    monkeypatch.setattr(internal_auth, "session_ok", lambda token: token == "good-session")
    person = {"Cookie": "synapse_session=good-session"}
    state = await _sign_in(client, headers=person)
    started = len(_Flow.made)
    for _ in range(12):
        refused = await client.get("/auth/login")
        assert not refused.headers.get("location", "").startswith("https://accounts.example/")
    assert len(_Flow.made) == started
    back = _back(await client.get("/auth/callback", params={"code": "c1", "state": state}))
    assert back == {"google_auth": "success"} and [t["token"] for t in kept] == ["tok-c1"]
