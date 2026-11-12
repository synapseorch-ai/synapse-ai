"""A held secret written with lookalikes is masked as it was written (F1339, TRI1; CAP3's FF17 gate, cap-02; Naveen
11-04: "Build as planned").

cap-02's player pasted a key the workspace holds with typographic hyphens (U+2011, as smart punctuation writes them).
It has no shape the scrubber knows, and a held value was matched by its exact text, so the door missed it and it reached
the builder agent's and orchestration's logs and the run's events. A held value is now matched through the door's own
lookalike fold (F1280's dashes, the no-break spaces, zero-width characters), on both sides, and the span is masked as
it stands in the text, the words around it untouched: at the door, on the output side, and in a run's named secrets.
Nothing else is matched: an em dash in prose, a near miss, a held value's first part.

Every secret below is assembled at run time: a literal one in a source file trips secret scanners.
"""
from __future__ import annotations

import pytest

from core import held_secrets, secret_scrub
from core.native_builder import door
from core.native_builder.door import CREDENTIAL_MARK

REAL = "shop-" + "real-key-" + "5521"
PHRASE = "harbor " + "gate " + "7741"
SAVED_TYPOGRAPHIC = "acme\u2011" + "saved\u2011" + "key\u20119031"
DASHES = ["\u2010", "\u2011", "\u2012", "\u2013", "\u2014", "\u2015", "\u2212", "\ufe58", "\ufe63", "\uff0d"]


@pytest.fixture
async def kept():
    """Where the workspace keeps them: a tool's key header, a pass phrase header, and a value saved with no-break
    hyphens."""
    from core.store.resources import save_tool

    await save_tool({"name": "shop_orders", "description": "The shop's orders.", "tool_type": "http", "method": "GET",
                     "url": "https://shop.example.com/orders",
                     "headers": {"X-Api-Key": REAL, "X-Phrase": PHRASE, "X-Acme": SAVED_TYPOGRAPHIC}})


class TestAtTheDoor:
    @pytest.mark.parametrize("dash", DASHES)
    async def test_each_dash_lookalike_is_masked_as_written(self, client, kept, dash):
        pasted = REAL.replace("-", dash)
        text = f"for the shop \u2014 {pasted} \u2014 thanks"
        held = await secret_scrub.held_in(text)
        assert held == [(pasted, "connection")], held
        checked = door.check(text, held)
        # Read folded, kept as written (F1551): the em dashes around it stay.
        assert checked.text == f"for the shop \u2014 {CREDENTIAL_MARK} \u2014 thanks" and checked.removed == 1, checked

    async def test_a_no_break_space_and_a_zero_width_character_hide_nothing(self, client, kept):
        for pasted in (PHRASE.replace(" ", "\u00a0"), REAL[:5] + "\u200b" + REAL[5:]):
            text = f"it is {pasted} for now"
            checked = door.check(text, await secret_scrub.held_in(text))
            assert checked.text == f"it is {CREDENTIAL_MARK} for now", (pasted, checked)

    async def test_one_saved_with_lookalikes_is_masked_when_pasted_plainly(self, client, kept):
        pasted = SAVED_TYPOGRAPHIC.replace("\u2011", "-")
        held = await secret_scrub.held_in(f"use {pasted} here")
        assert held == [(pasted, "connection")], held

    def test_the_door_folds_through_the_one_table(self):
        """It reads through the one table and keeps the message as written (F1551)."""
        assert getattr(secret_scrub, "LOOKALIKES", None) is door._LOOKALIKES
        assert door.check("Ship it\u00a0Monday \u2013 not Friday").text == "Ship it\u00a0Monday \u2013 not Friday"


class TestOnTheOutputSide:
    @pytest.mark.parametrize("dash", ["\u2011", "\u2013", "\u2212"])
    async def test_the_matcher_marks_it_as_written(self, client, kept, dash):
        pasted = REAL.replace("-", dash)
        matcher = await held_secrets.matcher()
        assert matcher is not None and matcher.find(f"sent with {pasted}.") == [pasted]
        await secret_scrub.refresh_held()
        assert secret_scrub.scrub_secrets(f"log: sent with {pasted}.") == f"log: sent with {CREDENTIAL_MARK}."

    def test_a_runs_named_secret_is_named_as_written(self):
        pasted = REAL.replace("-", "\u2011")
        assert secret_scrub.for_storage({"text": f"key {pasted}"}, held={"SHOP_KEY": REAL}) == {
            "text": f"key {secret_scrub.secret_mark('SHOP_KEY')}"}
        token = secret_scrub._HELD.set((("SHOP_KEY", REAL),))
        try:
            assert secret_scrub.scrub_secrets(f"key {pasted}") == f"key {secret_scrub.secret_mark('SHOP_KEY')}"
        finally:
            secret_scrub._HELD.reset(token)


class TestNothingElseIsMatched:
    @pytest.mark.parametrize("text", [
        "shop \u2014 real \u2014 key \u2014 5521",          # its words in prose, dashes between them
        "shop\u2011real\u2011key\u20115529",                 # a near miss
        "shop\u2011real\u2011key",                           # its first part
        "We are open Monday \u2013 Friday, 9\u201117.",      # lookalikes in ordinary prose
    ])
    async def test_it_is_left_alone(self, client, kept, text):
        assert await secret_scrub.held_in(text) == []
        matcher = await held_secrets.matcher()
        assert matcher.find(text) == []
