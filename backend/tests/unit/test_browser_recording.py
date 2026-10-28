"""Recording a task the person does once (BRW1, D328, F1141; DESIGN §v5.2): its steps, their sentences, and what is
never kept — a password, a card or a one-time code, typed or pictured (BRW3's PCI point)."""
from __future__ import annotations

import base64
import json

from core.browser import recording
from core.browser.recording import Recorder, line_of

ORDER = {"role": "textbox", "name": "Order number", "label": "Order number", "text": "", "css": "#order",
         "tag": "input", "type": "text", "autocomplete": "", "field": True}
PASSWORD = {**ORDER, "name": "Password", "label": "Password", "css": "#pw", "type": "password"}
CARD = {**ORDER, "name": "Card", "label": "Card number", "css": "#cc", "autocomplete": "cc-number"}
SEARCH = {"role": "button", "name": "Search", "label": "", "text": "Search", "css": "#go", "tag": "button",
          "type": "submit", "autocomplete": "", "field": False}


def test_each_step_reads_as_a_plain_sentence():
    assert line_of({"action": "open", "url": "https://portal.example.com/orders"}) == \
        "Open https://portal.example.com/orders"
    assert line_of({"action": "click", "target": SEARCH}) == "Click Search"
    assert line_of({"action": "type", "target": ORDER, "value": {"input": "order number"}}) == \
        "Type {order number} into Order number"
    assert line_of({"action": "type", "target": ORDER, "value": {"fixed": "A-1"}}) == "Type “A-1” into Order number"
    assert line_of({"action": "type", "target": PASSWORD, "value": {"person": "password"}}) == "You sign in here"
    assert line_of({"action": "type", "target": CARD, "value": {"person": "card"}}) == "You enter the card details"
    assert line_of({"action": "press", "key": "Enter"}) == "Press Enter"
    assert line_of({"action": "back"}) == "Go back"


def test_typing_into_one_field_is_one_step_and_a_click_into_a_field_is_none():
    rec = Recorder()
    rec.opened("https://portal.example.com/")
    assert rec.clicked(ORDER) is None, "putting the cursor in a field is not a step"
    rec.typed("A-", None)
    rec.typed("1001", None)
    rec.pressed("Enter")
    rec.pressed("Tab")  # only Enter is a step
    assert [s["action"] for s in rec.steps] == ["open", "type", "press"]
    assert rec.steps[1]["value"] == {"fixed": "A-1001"} and rec.steps[1]["line"] == "Type “A-1001” into Order number"
    assert [s["n"] for s in rec.steps] == [1, 2, 3] and len({s["id"] for s in rec.steps}) == 3


def test_a_password_a_card_or_a_code_is_never_kept():
    rec = Recorder()
    rec.typed("hunter2", PASSWORD)
    rec.typed("4111 1111 1111 1111", CARD)
    rec.typed("4111", ORDER)  # a card number typed into an ordinary field is caught as it grows
    rec.typed(" 1111 1111 1111", ORDER)
    values = [s["value"] for s in rec.steps]
    assert values == [{"person": "password"}, {"person": "card"}, {"person": "card"}]
    assert "hunter2" not in json.dumps(rec.result()) and "4111" not in json.dumps(rec.result())
    assert rec.frame()["holding"] == "card"


def test_no_picture_is_kept_of_a_page_something_of_the_persons_was_typed_on():
    rec = Recorder()
    rec.typed("hunter2", PASSWORD)
    rec.keep_quiet("https://portal.example.com/login")
    rec.shot(rec.steps[-1], b"\xff\xd8secret-in-view", "https://portal.example.com/login")
    rec.flush()
    later = rec.clicked(SEARCH)
    rec.shot(later, b"\xff\xd8still-same-page", "https://portal.example.com/login")
    assert not rec.shots, "no picture while held, nor after on the same page"
    after = rec.clicked(SEARCH)
    rec.shot(after, b"\xff\xd8next-page", "https://portal.example.com/orders")
    assert list(rec.shots.values()) == [b"\xff\xd8next-page"] and after["after_shot"] in rec.shots
    out = rec.result()
    assert base64.b64decode(out["shots"][after["after_shot"]]) == b"\xff\xd8next-page"


def test_a_full_recording_refuses_more_steps_and_says_so():
    rec = Recorder()
    for _ in range(recording.MAX_STEPS):
        rec.clicked(SEARCH)
    assert rec.clicked(SEARCH) is None and len(rec.steps) == recording.MAX_STEPS
    assert rec.frame()["full"] is True and "full" in rec.frame()["words"]
