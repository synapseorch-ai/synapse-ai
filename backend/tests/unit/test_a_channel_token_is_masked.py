"""A chat channel's token is masked wherever text is scrubbed (F1286, TRI1; 79's call 11-02: inside Naveen's 10-15
"shapes only" rule).

REACH2's rule 14 (D340) connects a Telegram, Discord or WhatsApp channel on a card, "where the person types the token,
never the chat". A person who pastes it into the chat anyway kept it: a Telegram bot token, a Discord bot token and
Meta's `EAA…` access token matched none of secret_scrub's shapes, alone or in a sentence, and F1280's rule masks a lone
key only at the Connect card's wait, which a channel's card is not. All three have fixed, published shapes, so they join
the shapes: masked by the door, a run's error, a log — everywhere `scrub_credentials` runs.

Every token below is assembled at run time: a literal one in a source file trips secret scanners.
"""
from __future__ import annotations

import pytest

from core.native_builder.door import CREDENTIAL_MARK, check
from core.secret_scrub import holds_credential, scrub_credentials

# A Telegram bot token: the bot's numeric id, a colon, then 35 characters (BotFather's shape).
TELEGRAM = "7123456789" + ":" + "AAH" + "k9Lm2Nq4Rs6Tv8Wx0Yz1Ab3Cd5Ef7Gh9"
# A Discord bot token: the bot's id in base64, a 6-character timestamp, a 27+ character HMAC, dot-separated.
DISCORD = "MTE5ODc2NTQzMjEwOTg3NjU0Mw" + "." + "GaBcDe" + "." + "q1W2e3R4t5Y6u7I8o9P0a1S2d3F4g5H6"
# Meta's access token (WhatsApp Cloud API, Graph API): EAA, then a long run of letters and digits.
META = "EAA" + "GbZB7qK9ZC3xP2mN8vL4rT6yW1sD5fH0jA7cE9gI2kM4oQ6uS8wY0bD3fH5jL7nP9rT1vX3zB5dF7hJ9lN1pR3tV5xZ7"


@pytest.mark.parametrize("token", [TELEGRAM, DISCORD, META], ids=["telegram", "discord", "meta"])
class TestEachShape:
    def test_alone(self, token):
        assert scrub_credentials(token) == (CREDENTIAL_MARK, 1)
        assert holds_credential(token)

    def test_in_a_sentence_at_the_door(self, token):
        checked = check(f"Connect our support bot, the token is here {token} thanks")
        assert token not in checked.text and checked.removed == 1
        assert checked.text == f"Connect our support bot, the token is here {CREDENTIAL_MARK} thanks"

    def test_in_a_run_error(self, token):
        from core.orchestration.run_error import clean

        assert token not in clean(f"401 Unauthorized for token {token}")


@pytest.mark.parametrize("text", [
    "Meet at 12:34 on Friday",
    "order 1234:abc shipped",
    "run 20261102:step4 failed",
    "bot 123456:short",
    "ratio 16:9 and 4:3",
    "see https://example.com/a:b",
    "the build id is MTE5ODc2NTQz and the tag is v2.0.1",
    "a.b.c and x.y.z are versions",
    "EAA is the airline's code; EAAB too",
    "the hash is " + "d41d8cd98f00b204e9800998ecf8427e" + " in base64 " + "aGVsbG8gd29ybGQgdGhpcyBpcyBwcm9zZQ==",
], ids=["time", "id", "run-step", "short-token", "ratios", "url", "plain-id-and-version", "dotted-words",
        "eaa-word", "hash-and-base64"])
def test_what_only_looks_close_is_left_alone(text):
    assert scrub_credentials(text) == (text, 0)


def test_a_jwt_is_still_masked_once():
    jwt = "eyJ" + "hbGciOiJIUzI1NiJ9" + "." + "eyJzdWIiOiIxMjM0In0" + "." + "abcdefghijkl1234"
    assert scrub_credentials(f"auth {jwt}") == (f"auth {CREDENTIAL_MARK}", 1)


def test_a_discord_shape_is_tight():
    """Three dot-joined runs that are not a bot token's lengths stay: only id(23–28).ts(6).hmac(27–38)."""
    near = ["MTE5ODc2NTQz" + "." + "GaBcDe" + "." + "q1W2e3R4t5Y6u7I8o9P0a1S2d3F4g5H6",   # id too short
            "MTE5ODc2NTQzMjEwOTg3NjU0Mw" + "." + "GaBc" + "." + "q1W2e3R4t5Y6u7I8o9P0a1S2d3F4g5H6",  # ts too short
            "MTE5ODc2NTQzMjEwOTg3NjU0Mw" + "." + "GaBcDe" + "." + "q1W2e3R4t5Y6"]  # hmac too short
    for text in near:
        assert scrub_credentials(text) == (text, 0), text
