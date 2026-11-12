"""What a person pastes reaches the builder as they wrote it; every key it holds is still masked — F1551 (BLD3; Naveen's
pick 11-12, on F1550's last difference).

F1280 (prod-02: a key typed with U+2011 hyphens went past every pattern) made the door fold the whole message through
`secret_scrub.LOOKALIKES` before reading it: every dash to "-", every no-break space to a space, every zero-width
character and soft hyphen gone, and the folded text was what ran. Since F1386 every credential shape reads the text
folded and masks what it finds as it was written, so the fold need not touch what is kept: on BLD3's 46 KB export
(F1550) it was the one difference left between the pasted instructions and the saved workflow.

Now the door reads folded (cards, passwords, every key shape, a lone key at a Connect card or while one is asked, a
value after a bare "key") and keeps the message as it was written, each key masked where it stands. Read folded, the
kept text is exactly what the door kept before, for every key shape with every lookalike; and a key hidden with any of
them leaves no part of itself behind.
"""
from __future__ import annotations

import pytest

from core.native_builder import door, export_state
from core.native_builder.export import agent_instructions
from core.secret_scrub import CREDENTIAL_MARK, LOOKALIKES, fold, scrub_credentials

DASHES = [chr(c) for c in (0x2010, 0x2011, 0x2012, 0x2013, 0x2014, 0x2015, 0x2212, 0xFE58, 0xFE63, 0xFF0D,
                           0x2043, 0xFE31, 0xFE32, 0x2E3A, 0x2E3B, 0x02D7)]
SPACES = [chr(c) for c in (0x00A0, 0x2007, 0x202F)]
INVISIBLE = [chr(c) for c in (0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF, 0x00AD)]
EM, EN, NBSP = chr(0x2014), chr(0x2013), chr(0x00A0)
TAIL = "abcdefghijkl0123456789AB"
#: Prose with every lookalike the table folds: what a pasted prompt or export is full of.
PROSE = (f"Pages 3{EN}5 {EM} reply{NBSP}today. " + " ".join(f"a{d}b" for d in DASHES) + " "
         + " ".join(f"c{s}d" for s in SPACES) + " " + " ".join(f"e{z}f" for z in INVISIBLE) + ".")


def _keys(d: str) -> list[tuple[str, str]]:
    """(a key shape written with `d` for its hyphens, its secret part): every shape the door's patterns mask."""
    return [
        (f"My key sk{d}proj{d}{TAIL} works", TAIL),
        (f"token xoxb{d}1234567890{d}abcdefGHIJ12 here", "abcdefGHIJ12"),
        (f"Authorization: Bearer abcd1234{d}efgh5678{d}ijkl9012", "ijkl9012"),
        (f"the API key is abc123{d}def456{d}ghi789.", "ghi789"),
        (f"curl -H 'X{d}Api{d}Key: ab12{d}cd34{d}ef56{d}gh78' https://api.example.com/v1", "gh78"),
    ]


def _before(text: str) -> tuple[str, int]:
    """What the door kept before F1551, for a message with no held secret and no card up: the folded text, its keys
    masked."""
    return scrub_credentials(text.translate(LOOKALIKES))


class TestTheTextIsKeptAsWritten:
    def test_prose_with_every_lookalike(self):
        checked = door.check(PROSE)
        assert (checked.text, checked.removed, checked.refused) == (PROSE, 0, None)

    def test_an_answer_too(self):
        assert door.check_answer({"feedback": PROSE}, "What should change?") == ({"feedback": PROSE}, 0)
        assert door.check_answer({"feedback": PROSE}, "Paste the key on the card.", step="step_wait_connect") == (
            {"feedback": PROSE}, 0)

    def test_the_text_around_a_key(self):
        key = f"sk{DASHES[1]}proj{DASHES[1]}{TAIL}"
        checked = door.check(f"Pages 3{EN}5 {EM} {key} {EM} done{NBSP}today")
        assert checked.text == f"Pages 3{EN}5 {EM} {CREDENTIAL_MARK} {EM} done{NBSP}today" and checked.removed == 1

    def test_a_pasted_exports_instructions_reach_the_build_as_written(self):
        instructions = f"You keep the cart {EM} in PYG{NBSP}only {EN} and say{chr(0x2011)}so."
        code = (f'from agents import Agent\n\ncart = Agent(name="Cart Agent", instructions="""{instructions}""")\n')
        kept = door.check("Rebuild this OpenAI Agent Builder workflow here:\n\n" + code).text
        assert agent_instructions(export_state(kept)["export_source"]) == {"Cart Agent": instructions}


class TestEveryKeyIsStillMasked:
    @pytest.mark.parametrize("d", DASHES, ids=lambda d: hex(ord(d)))
    def test_every_shape_with_every_dash_as_before(self, d):
        for said, secret in _keys(d):
            text = f"{EM} {said} {EM}{NBSP}thanks"
            checked = door.check(text)
            assert (fold(checked.text), checked.removed) == _before(text), (hex(ord(d)), said, checked)
            assert secret not in checked.text and CREDENTIAL_MARK in checked.text, (hex(ord(d)), checked.text)
            assert checked.text.startswith(f"{EM} ") and checked.text.endswith(f"{EM}{NBSP}thanks"), checked.text

    @pytest.mark.parametrize("z", INVISIBLE, ids=lambda z: hex(ord(z)))
    def test_an_invisible_character_inside_a_key_hides_nothing(self, z):
        key = f"sk-proj-{TAIL[:6]}{z}{TAIL[6:]}"
        checked = door.check(f"use {key} {EM} now")
        assert checked.text == f"use {CREDENTIAL_MARK} {EM} now" and checked.removed == 1, checked
        stripe = "sk_" + "test_" + "Z" * 10 + z + "Z" * 14
        assert door.check(f"key: {stripe}").removed == 1

    @pytest.mark.parametrize("d", [DASHES[1], DASHES[3], DASHES[6]], ids=lambda d: hex(ord(d)))
    def test_a_lone_key_at_the_connect_wait_and_with_a_card_up(self, d):
        canary = d.join(["tidewell", "canary", "key", "2284"])
        assert door.check_answer({"feedback": canary}, None, step="step_wait_connect") == (
            {"feedback": CREDENTIAL_MARK}, 1)
        assert door.check(canary, card_up=True).text == CREDENTIAL_MARK

    @pytest.mark.parametrize("d", [DASHES[1], DASHES[4], INVISIBLE[0]], ids=lambda d: hex(ord(d)))
    def test_a_value_after_a_bare_key_while_a_key_is_asked(self, d):
        value = d.join(["amberfox", "river4821", "lake9031"])
        masked, removed = door.check_answer({"answer": f"use key {value} {EM} thanks"}, "What is the API key?")
        assert masked == {"answer": f"use key {CREDENTIAL_MARK} {EM} thanks"} and removed == 1, masked

    @pytest.mark.parametrize("text", [
        f"card 4111{DASHES[1]}1111{DASHES[1]}1111{DASHES[1]}1111 please",
        f"card 4111{NBSP}1111{NBSP}1111{NBSP}1111",
        f"pay with 4111{INVISIBLE[0]}111111111111",
    ])
    def test_a_card_number_written_with_lookalikes_is_refused(self, text):
        assert door.check(text).refused == "card"

    def test_a_password_written_with_lookalikes_is_refused(self):
        assert door.check(f"my pass{chr(0x00AD)}word is Hunter{DASHES[1]}2x9").refused == "password"
