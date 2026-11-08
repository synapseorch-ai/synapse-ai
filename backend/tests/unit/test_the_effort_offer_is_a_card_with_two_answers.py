"""The panel draws the effort offer and its press names its turn (F1154, SAI2-P3, D300; the synced reducer and the
self-hosted panel; cloud's own panel has vitest)."""
from __future__ import annotations


def test_the_panel_draws_the_offer_and_its_press_names_its_turn():
    """F1154, the synced reducer and the self-hosted panel (cloud's has vitest): the offer is a card under the turn's
    words; "Use" sends `effort_of`, never a model; "Continue" is a message like any other; any new message takes the
    card down."""
    from pathlib import Path

    src = Path(__file__).resolve().parents[3] / "frontend" / "src"
    events = (src / "lib" / "builderEvents.ts").read_text()
    assert "case 'effort_offer':" in events and "effortOffer: event as EffortOffer" in events
    assert "return use ? { text: offer.use, effortOf: offer.effort_of } : { text: offer.continue };" in events
    panel = (src / "components" / "app" / "BuilderPanel.tsx").read_text()
    assert "message.kind === 'effort_offer' && message.effortOffer" in panel and "effortAnswer(offer, use)" in panel
    chats = (src / "components" / "app" / "builderChats.ts").read_text()
    assert "...(options.effortOf && !retryOf ? { effort_of: options.effortOf } : {})," in chats
    assert "m.kind !== 'try_stronger' && m.kind !== 'effort_offer'" in chats
