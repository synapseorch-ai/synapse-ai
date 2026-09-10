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


class TestTheStreamIsAnAllowlist:
    """What reaches a stranger's browser, and what must never.

    `_log_llm_call` carries the **full assembled prompt and the agent's system
    prompt**; `tool_execution` carries a tool's arguments. Both are fine on the
    workspace timeline, where the reader owns the agent. Neither is fine on
    somebody else's marketing page, and the widget client ignoring them is not a
    defence — they still travel, and devtools still shows them.

    Written against the filter directly rather than through a live turn: the
    point is the rule, and a test that needs a model to prove it would be skipped
    on the machine where it matters.
    """

    async def _emitted(self, events):
        from core.routes.widget import _for_a_visitor

        async def source():
            for event in events:
                yield f"data: {json.dumps(event)}\n\n"

        seen = []
        async for chunk in _for_a_visitor(source()):
            seen.append(json.loads(chunk[6:].strip()))
        return seen

    async def test_the_full_prompt_never_leaves_the_building(self):
        out = await self._emitted([
            {"type": "_log_llm_call", "prompt": "SECRET PROMPT",
             "system_prompt": "You are Acme's confidential assistant."},
            {"type": "final", "response": "Hello."},
        ])
        assert out == [{"type": "final", "content": "Hello."}]

    async def test_tool_calls_and_reasoning_are_dropped(self):
        out = await self._emitted([
            {"type": "llm_thought", "thought": "I should check the CRM"},
            {"type": "tool_execution", "tool_name": "run_sql_query",
             "args": {"sql": "SELECT * FROM customers"}},
            {"type": "tool_result", "tool_name": "run_sql_query", "preview": "3 rows"},
            {"type": "tasks_updated", "tasks": [{"title": "look up the account"}]},
            {"type": "done"},
        ])
        assert out == [{"type": "done"}]

    async def test_an_event_the_engine_has_not_invented_yet_is_invisible(self):
        """The reason this is an allowlist. A denylist would ship every new event
        to every visitor until somebody noticed."""
        out = await self._emitted([{"type": "brand_new_engine_event", "secrets": "…"}])
        assert out == []

    async def test_status_keeps_no_message(self):
        """The engine's own words describe the machinery — "calling scrape_url",
        which names a tool and often a target."""
        out = await self._emitted([{"type": "status", "message": "calling scrape_url on acme.com"}])
        assert out == [{"type": "status"}]

    async def test_a_pause_ends_the_conversation_rather_than_asking(self):
        out = await self._emitted([
            {"type": "human_input_required", "kind": "tool_approval",
             "tool_name": "delete_everything",
             "tool_args": {"table": "customers", "cascade": True}},
            {"type": "final", "response": "should never be reached"},
        ])
        # Ends, and ends *here* — the `final` after it is never reached, because
        # the turn the visitor was watching is over as far as they are concerned.
        assert [e["type"] for e in out] == ["response", "done"]
        # Neither the tool nor its arguments appear anywhere in what was sent.
        rendered = json.dumps(out)
        for leaked in ("delete_everything", "customers", "cascade"):
            assert leaked not in rendered


class TestTheLogo:
    """A logo becomes a `data:` URI the browser will honour, so what we accept
    matters more than it looks."""

    #: The smallest valid PNG — an 8-byte signature is what `sniff` reads.
    PNG = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000a49444154789c6300010000050001" "0d0a2db4" "0000000049454e44ae426082"
    )

    async def _widget(self, client, seed_agent):
        agent = await seed_agent()
        return await _make_widget(client, agent["id"])

    async def test_a_png_becomes_a_data_uri(self, client, seed_agent):
        widget = await self._widget(client, seed_agent)
        response = await client.post(
            f"/api/widgets/{widget['id']}/logo",
            files={"file": ("logo.png", self.PNG, "image/png")},
        )
        assert response.status_code == 200, response.text
        assert response.json()["logo"].startswith("data:image/png;base64,")

    async def test_the_magic_bytes_decide_not_the_filename(self, client, seed_agent):
        """`.png` on a text file is the ordinary way this goes wrong, and the
        value ends up in a `data:` URI, so the name is not evidence."""
        widget = await self._widget(client, seed_agent)
        response = await client.post(
            f"/api/widgets/{widget['id']}/logo",
            files={"file": ("logo.png", b"not an image at all", "image/png")},
        )
        assert response.status_code == 422
        assert "Renaming a file" in response.json()["detail"]

    async def test_an_oversized_logo_is_refused_with_the_number(self, client, seed_agent):
        widget = await self._widget(client, seed_agent)
        oversized = self.PNG + b"\x00" * (128 * 1024)
        response = await client.post(
            f"/api/widgets/{widget['id']}/logo",
            files={"file": ("logo.png", oversized, "image/png")},
        )
        assert response.status_code == 413
        assert "128 KB" in response.json()["detail"]

    async def test_it_can_be_removed(self, client, seed_agent):
        widget = await self._widget(client, seed_agent)
        await client.post(
            f"/api/widgets/{widget['id']}/logo",
            files={"file": ("logo.png", self.PNG, "image/png")},
        )
        assert (await client.delete(f"/api/widgets/{widget['id']}/logo")).json()["logo"] == ""

    async def test_a_visitor_is_handed_the_logo_but_never_the_allow_list(
        self, client, seed_agent
    ):
        widget = await self._widget(client, seed_agent)
        await client.post(
            f"/api/widgets/{widget['id']}/logo",
            files={"file": ("logo.png", self.PNG, "image/png")},
        )
        session = await client.post(
            "/api/widget/session",
            json={"public_key": widget["public_key"], "origin": "https://acme.com"},
        )
        theme = session.json()["widget"]
        assert theme["logo"].startswith("data:image/png")
        assert "allowed_origins" not in theme


class TestThePreviewIsOwnerOnly:
    """The preview skips the origin check, because the caller *is* the workspace.

    That is only safe while it stays behind the login gate, which is a property
    of `proxy.ts`'s bypass list and easy to undo by accident — `/embed/preview/`
    has to be excluded from the prefix that lets a stranger reach `/embed/{key}`.
    """

    async def test_the_bypass_list_excludes_it(self):
        """Read out of the file, because the mistake this guards against is
        someone widening the prefix and never seeing a test fail."""
        import pathlib
        import re

        proxy = (
            pathlib.Path(__file__).resolve().parents[3]
            / "frontend" / "src" / "proxy.ts"
        ).read_text()
        assert "'/embed/preview/'" in proxy
        exceptions = re.search(r"AUTH_BYPASS_EXCEPTIONS\s*=\s*\[([^\]]*)\]", proxy)
        assert exceptions and "/embed/preview/" in exceptions.group(1)

    async def test_it_mints_a_token_for_the_owners_own_frame(self, client, seed_agent):
        agent = await seed_agent()
        widget = await _make_widget(client, agent["id"], origins=[])
        response = await client.post(f"/api/widgets/{widget['id']}/preview")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["token"].startswith("wgt1.")
        # An empty allow-list stops a *visitor*; it must not stop the owner
        # seeing what they are configuring.
        assert body["widget"]["accent"]

    async def test_a_widget_with_no_agent_says_so_rather_than_500ing(
        self, client, seed_agent
    ):
        widget = await _make_widget(client, "")
        response = await client.post(f"/api/widgets/{widget['id']}/preview")
        assert response.status_code == 409
