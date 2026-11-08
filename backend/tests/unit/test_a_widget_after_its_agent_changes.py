"""A widget's returning visitor gets a new chat after the owner changes its agent (F278).

`PUT /api/widgets/{id}` with a new agent: every call of a visitor who already
had a conversation answered 409 "This chat has been updated. Reload the page.",
the widget showed only its greeting, the typed message vanished, and reloading
did not help — the frame refreshed its stored token, which still named the old
agent, and dropped the fresh one the page had just minted.

Now the refusal says which 409 it is (`code: "widget_updated"`, beside the same
sentence), a refresh of such a token is refused the same way so a reload keeps
the fresh token, and `/api/widget/restart` starts a new conversation for the
widget's current agent from the old token — still bound to its origin.
"""
import pytest

from core.chat_widgets import limits as _limits
from core.chat_widgets import store as _store

ORIGIN = "https://acme.com"


@pytest.fixture(autouse=True)
def _clean_limits():
    _limits.reset()
    yield
    _limits.reset()


async def _visitor_on_a_repointed_widget(client, seed_agent):
    from _fakes import seed as S
    first, second = S.make_agent(id="agent_f278_first", name="First"), S.make_agent(id="agent_f278_second", name="Second")
    await S.seed_agents([first, second])   # one write: seeding replaces the list
    made = await client.post("/api/widgets", json={"name": "Support", "agent_id": first["id"],
                                                    "allowed_origins": [ORIGIN]})
    widget = made.json()
    session = (await client.post("/api/widget/session",
                                 json={"public_key": widget["public_key"], "origin": ORIGIN})).json()
    stored = await _store.get_widget(widget["id"])
    stored["agent_id"] = second["id"]
    await _store.save_widget(stored)
    return widget, session, second


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


class TestTheRefusalSaysWhatHappened:
    async def test_every_door_names_it(self, client, seed_agent):
        _, session, _ = await _visitor_on_a_repointed_widget(client, seed_agent)
        for method, path, body in (("POST", "/api/widget/chat", {"message": "hi"}),
                                   ("GET", "/api/widget/history", None),
                                   ("GET", "/api/widget/stream", None)):
            resp = await client.request(method, path, json=body, headers=_bearer(session["token"]))
            assert resp.status_code == 409, path
            assert resp.json()["code"] == "widget_updated", path
            assert resp.json()["detail"] == "This chat has been updated. Reload the page.", path

    async def test_a_reload_does_not_bring_the_old_conversation_back(self, client, seed_agent):
        _, session, _ = await _visitor_on_a_repointed_widget(client, seed_agent)
        resp = await client.post("/api/widget/refresh", headers=_bearer(session["token"]))
        assert resp.status_code == 409 and resp.json()["code"] == "widget_updated"


class TestRestart:
    async def test_a_new_conversation_with_the_current_agent(self, client, seed_agent):
        widget, session, second = await _visitor_on_a_repointed_widget(client, seed_agent)
        resp = await client.post("/api/widget/restart", headers=_bearer(session["token"]))
        assert resp.status_code == 200, resp.text
        fresh = resp.json()
        assert fresh["session_id"] != session["session_id"]
        history = await client.get("/api/widget/history", headers=_bearer(fresh["token"]))
        assert history.status_code == 200 and history.json()["messages"] == []

        from core.widget_token import verify
        from core.routes.widget import _secret
        claims = verify(fresh["token"], secret=_secret())
        assert claims.agent_id == second["id"] and claims.origin == ORIGIN
        assert claims.widget_id == widget["id"]

    async def test_it_needs_a_real_token(self, client, seed_agent):
        await _visitor_on_a_repointed_widget(client, seed_agent)
        resp = await client.post("/api/widget/restart", headers=_bearer("wgt1.forged.sig"))
        assert resp.status_code == 401

    async def test_a_switched_off_widget_or_a_dropped_origin_gets_none(self, client, seed_agent):
        widget, session, _ = await _visitor_on_a_repointed_widget(client, seed_agent)
        stored = await _store.get_widget(widget["id"])
        stored["allowed_origins"] = ["https://other.example"]
        await _store.save_widget(stored)
        assert (await client.post("/api/widget/restart", headers=_bearer(session["token"]))).status_code == 403
        stored["allowed_origins"] = [ORIGIN]
        stored["enabled"] = False
        await _store.save_widget(stored)
        assert (await client.post("/api/widget/restart", headers=_bearer(session["token"]))).status_code == 403

    async def test_it_is_limited_like_a_new_session(self, client, seed_agent, monkeypatch):
        _, session, _ = await _visitor_on_a_repointed_widget(client, seed_agent)
        # One per window, and the page's own /session already spent it: the
        # restart is counted in the same bucket (0 would mean no limit).
        monkeypatch.setattr(_limits, "SESSION_PER_IP", ("sess", 1, 300))
        resp = await client.post("/api/widget/restart", headers=_bearer(session["token"]))
        assert resp.status_code == 429
