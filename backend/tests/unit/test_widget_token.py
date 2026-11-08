"""The visitor token: what it proves, and what it must refuse to prove.

There is no server-side row behind this token — the claims are the state and the
HMAC is what makes them unforgeable. That buys a stateless widget and costs
revocation, so the two properties worth pinning are that a tampered token is
indistinguishable from a malformed one, and that the session id inside it cannot
be chosen by whoever presents it.
"""
import time

import pytest

from core import widget_token

SECRET = "a-test-signing-key"


class TestRoundTrip:
    def test_what_goes_in_comes_back(self):
        token, session = widget_token.mint(
            "wgt_1", "agent_7", origin="https://acme.com", secret=SECRET
        )
        claims = widget_token.verify(token, secret=SECRET)
        assert claims is not None
        assert (claims.widget_id, claims.agent_id, claims.origin) == (
            "wgt_1",
            "agent_7",
            "https://acme.com",
        )
        assert claims.session_id == session

    def test_a_session_id_is_minted_not_accepted(self):
        """A conversation id the client can choose is a transcript the client can
        guess. Two mints with no session id must never collide."""
        _, first = widget_token.mint("wgt_1", "a", origin="https://acme.com", secret=SECRET)
        _, second = widget_token.mint("wgt_1", "a", origin="https://acme.com", secret=SECRET)
        assert first != second
        assert first.startswith("wgtc_")

    def test_continuing_a_conversation_keeps_its_id(self):
        token, _ = widget_token.mint(
            "wgt_1", "a", origin="https://acme.com", secret=SECRET, session_id="wgtc_kept"
        )
        assert widget_token.verify(token, secret=SECRET).session_id == "wgtc_kept"


class TestRefusals:
    """Every one of these returns `None`, not a reason.

    A caller that distinguished 'expired' from 'forged' would be an oracle for
    somebody probing with a scraped key.
    """

    def test_a_tampered_payload_is_refused(self):
        token, _ = widget_token.mint("wgt_1", "a", origin="https://acme.com", secret=SECRET)
        prefix, body, signature = token.split(".")
        # Re-encode a claim set that says a different widget, keeping the old
        # signature — the forgery a stateless token has to survive.
        import base64, json
        forged = base64.urlsafe_b64encode(
            json.dumps({"w": "wgt_other", "a": "a", "s": "x", "o": "https://acme.com",
                        "iat": 0, "exp": 9_999_999_999}).encode()
        ).decode().rstrip("=")
        assert widget_token.verify(f"{prefix}.{forged}.{signature}", secret=SECRET) is None

    def test_another_deployments_key_is_refused(self):
        token, _ = widget_token.mint("wgt_1", "a", origin="https://acme.com", secret=SECRET)
        assert widget_token.verify(token, secret="a-different-key") is None

    def test_an_expired_token_is_refused(self):
        past = int(time.time()) - 7200
        token, _ = widget_token.mint(
            "wgt_1", "a", origin="https://acme.com", secret=SECRET, ttl=60, now=past
        )
        assert widget_token.verify(token, secret=SECRET) is None

    @pytest.mark.parametrize(
        "raw",
        ["", "not-a-token", "wgt1.only-two", "mgt1.abc.def", "wgt1..", "a.b.c.d"],
    )
    def test_junk_is_refused_without_raising(self, raw):
        assert widget_token.verify(raw, secret=SECRET) is None

    def test_a_gateway_token_is_not_a_widget_token(self):
        """Different prefixes so one signing surface can never be replayed as
        another, even if the two secrets were ever misconfigured to match."""
        token, _ = widget_token.mint("wgt_1", "a", origin="https://acme.com", secret=SECRET)
        assert token.startswith("wgt1.")


class TestRefresh:
    def test_a_just_expired_token_can_be_traded_in(self):
        past = int(time.time()) - 120
        token, session = widget_token.mint(
            "wgt_1", "a", origin="https://acme.com", secret=SECRET, ttl=60, now=past
        )
        fresh = widget_token.refresh(token, secret=SECRET)
        assert fresh is not None
        claims = widget_token.verify(fresh, secret=SECRET)
        assert claims.session_id == session
        assert claims.origin == "https://acme.com"

    def test_a_long_dead_token_cannot(self):
        """A tab left open over lunch works; one left open for a week does not."""
        ancient = int(time.time()) - widget_token.REFRESH_GRACE_SECONDS - 3600
        token, _ = widget_token.mint(
            "wgt_1", "a", origin="https://acme.com", secret=SECRET, ttl=60, now=ancient
        )
        assert widget_token.refresh(token, secret=SECRET) is None

    def test_refresh_cannot_change_the_origin_or_the_widget(self):
        """The refresh path skips the origin check, so it must not be a way to
        re-point a token at somewhere it was never issued for."""
        token, _ = widget_token.mint("wgt_1", "a", origin="https://acme.com", secret=SECRET)
        claims = widget_token.verify(widget_token.refresh(token, secret=SECRET), secret=SECRET)
        assert (claims.widget_id, claims.origin) == ("wgt_1", "https://acme.com")


class TestPublicKey:
    def test_it_is_random_rather_than_sequential(self):
        """Not a secret, but it must not be enumerable: a walkable list of keys
        is a list of which sites run a widget."""
        keys = {widget_token.new_public_key() for _ in range(50)}
        assert len(keys) == 50
        assert all(k.startswith("pk_") and len(k) > 20 for k in keys)
