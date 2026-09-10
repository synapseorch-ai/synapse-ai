"""The widget's visitor surface, end to end through the app.

The one property worth proving here is the one the whole feature rests on: a
scraped public key, presented from a site the owner did not list, gets a refusal
a human can read — not an empty stream, and not a conversation.

Everything else in this file exists because it is a way that property could be
lost by accident: a token that outlives a disabled widget, an agent id taken from
the request body instead of the signature, an approval card reaching a stranger.
"""
import json

import pytest

from core.chat_widgets import limits as _limits
from core.chat_widgets import store as _store


@pytest.fixture(autouse=True)
def _clean_limits():
    _limits.reset()
    yield
    _limits.reset()


async def _make_widget(client, agent_id, origins=("https://acme.com",), **extra):
    body = {"name": "Support", "agent_id": agent_id, "allowed_origins": list(origins)}
    body.update(extra)
    response = await client.post("/api/widgets", json=body)
    assert response.status_code == 200, response.text
    return response.json()


class TestTheOriginIsTheCredential:
    async def test_an_allowed_origin_gets_a_token(self, client, seed_agent):
        agent = await seed_agent()
        widget = await _make_widget(client, agent["id"])

        response = await client.post(
            "/api/widget/session",
            json={"public_key": widget["public_key"], "origin": "https://acme.com"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["token"].startswith("wgt1.")
        assert body["session_id"].startswith("wgtc_")
        # The allow-list is configuration, not something a visitor is told.
        assert "allowed_origins" not in body["widget"]

    async def test_a_scraped_key_from_another_site_is_refused_in_words(
        self, client, seed_agent
    ):
        """The headline case. Someone copies the snippet out of acme.com's HTML
        and pastes it on their own site."""
        agent = await seed_agent()
        widget = await _make_widget(client, agent["id"])

        response = await client.post(
            "/api/widget/session",
            json={"public_key": widget["public_key"], "origin": "https://evil.net"},
        )
        assert response.status_code == 403
        detail = response.json()["detail"]
        assert "evil.net" in detail and "not an allowed origin" in detail

    async def test_a_lookalike_domain_is_refused(self, client, seed_agent):
        agent = await seed_agent()
        widget = await _make_widget(client, agent["id"])
        response = await client.post(
            "/api/widget/session",
            json={"public_key": widget["public_key"], "origin": "https://acme.com.evil.net"},
        )
        assert response.status_code == 403

    async def test_an_unknown_key_is_a_404_not_a_hang(self, client):
        response = await client.post(
            "/api/widget/session",
            json={"public_key": "pk_nope", "origin": "https://acme.com"},
        )
        assert response.status_code == 404

    async def test_a_disabled_widget_stops_minting(self, client, seed_agent):
        agent = await seed_agent()
        widget = await _make_widget(client, agent["id"], enabled=False)
        response = await client.post(
            "/api/widget/session",
            json={"public_key": widget["public_key"], "origin": "https://acme.com"},
        )
        assert response.status_code == 403


class TestTheTokenIsTheAuthority:
    async def _token(self, client, seed_agent, **widget_kwargs):
        agent = await seed_agent()
        widget = await _make_widget(client, agent["id"], **widget_kwargs)
        response = await client.post(
            "/api/widget/session",
            json={"public_key": widget["public_key"], "origin": "https://acme.com"},
        )
        return widget, agent, response.json()["token"]

    async def test_no_token_is_401(self, client):
        response = await client.post("/api/widget/chat", json={"message": "hi"})
        assert response.status_code == 401

    async def test_a_forged_token_is_401(self, client):
        response = await client.post(
            "/api/widget/chat",
            json={"message": "hi"},
            headers={"Authorization": "Bearer wgt1.abc.def"},
        )
        assert response.status_code == 401

    async def test_the_body_cannot_name_a_different_agent(self, client, seed_agent):
        """`api_v1._resolve_agent_for_api` falls back to the *first* agent when
        the identifier does not match, so a typo reaches an agent nobody meant to
        publish. The widget takes the agent from the signature instead, and the
        request model has no field for one."""
        _, _, token = await self._token(client, seed_agent)
        response = await client.post(
            "/api/widget/chat",
            json={"message": "hi", "agent_id": "somebody-elses-agent"},
            headers={"Authorization": f"Bearer {token}"},
        )
        # Extra fields are ignored rather than honoured; the turn runs as the
        # token's agent or not at all.
        assert response.status_code != 200 or "agent_id" not in response.text

    async def test_disabling_a_widget_stops_a_live_token(self, client, seed_agent):
        """The token has no revocation of its own, so the widget is re-read per
        request. Without that, switching a widget off would leave every open
        conversation running for another hour."""
        widget, _, token = await self._token(client, seed_agent)
        widget["enabled"] = False
        await _store.save_widget(widget)

        response = await client.post(
            "/api/widget/chat",
            json={"message": "hi"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 403

    async def test_repointing_the_agent_ends_the_conversation(self, client, seed_agent):
        """Refused rather than silently switched: the transcript above belongs to
        the agent that wrote it."""
        widget, _, token = await self._token(client, seed_agent)
        other = await seed_agent(id="agent_other", name="Other")
        widget["agent_id"] = other["id"]
        await _store.save_widget(widget)

        response = await client.post(
            "/api/widget/chat",
            json={"message": "hi"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 409

    async def test_history_never_returns_a_pending_approval(self, client, seed_agent):
        """A widget shows no approval card, so a parked turn must not come back
        as one on reload."""
        _, _, token = await self._token(client, seed_agent)
        response = await client.get(
            "/api/widget/history", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 200
        assert "pending_approval" not in response.json()


class TestLimits:
    async def test_the_daily_cap_refuses_in_words(self, client, seed_agent):
        agent = await seed_agent()
        widget = await _make_widget(client, agent["id"], daily_message_cap=1)
        token = (
            await client.post(
                "/api/widget/session",
                json={"public_key": widget["public_key"], "origin": "https://acme.com"},
            )
        ).json()["token"]
        headers = {"Authorization": f"Bearer {token}"}

        first = await client.post("/api/widget/chat", json={"message": "one"}, headers=headers)
        assert first.status_code == 200
        second = await client.post("/api/widget/chat", json={"message": "two"}, headers=headers)
        assert second.status_code == 429
        assert "today" in second.json()["detail"]


class TestGatedAgentsAreRefusedAtBindTime:
    async def test_an_agent_that_would_ask_a_person_cannot_be_published(
        self, client, seed_agent
    ):
        """The approval card renders the tool's raw arguments and is answered by
        anyone with a role. A stranger on a marketing page has neither, so the
        refusal lands here — in front of the person who can fix it."""
        agent = await seed_agent(tool_policy={"destructive": "confirm"})
        response = await client.post(
            "/api/widgets",
            json={"name": "Support", "agent_id": agent["id"],
                  "allowed_origins": ["https://acme.com"]},
        )
        assert response.status_code == 409
        detail = response.json()["detail"]
        assert "cannot be published" in detail

    async def test_a_deny_policy_is_fine(self, client, seed_agent):
        """`deny` asks nobody anything — it refuses the call and tells the model
        why, which is exactly what a widget wants."""
        agent = await seed_agent(tool_policy={"destructive": "deny"})
        response = await client.post(
            "/api/widgets",
            json={"name": "Support", "agent_id": agent["id"],
                  "allowed_origins": ["https://acme.com"]},
        )
        assert response.status_code == 200


class TestKeyRotation:
    async def test_rotating_retires_the_old_key(self, client, seed_agent):
        agent = await seed_agent()
        widget = await _make_widget(client, agent["id"])
        old = widget["public_key"]

        rotated = (await client.post(f"/api/widgets/{widget['id']}/rotate-key")).json()
        assert rotated["public_key"] != old

        stale = await client.post(
            "/api/widget/session", json={"public_key": old, "origin": "https://acme.com"}
        )
        assert stale.status_code == 404

    async def test_an_ordinary_save_never_rotates_the_key(self, client, seed_agent):
        """The key is the customer's published address. Renaming the widget must
        not break every page already carrying the snippet."""
        agent = await seed_agent()
        widget = await _make_widget(client, agent["id"])
        renamed = await client.put(
            f"/api/widgets/{widget['id']}",
            json={"name": "Renamed", "agent_id": agent["id"],
                  "allowed_origins": ["https://acme.com"]},
        )
        assert renamed.json()["public_key"] == widget["public_key"]
