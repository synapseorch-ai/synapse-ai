"""The AI builder's door (BWA1, D274): a card number or a password is refused, a key is masked.

Measured on cloud dev (BWA1's Nova Pro baseline): the builder asked people to paste a portal password, an OpenAI
key and GitHub and Linear tokens into the chat, and each one, like a pasted card number, then sat in the run's state,
its events and its checkpoints. These are the guarantees of `core.native_builder.door`, the code that now stands in
front of every message and every answer the builder receives:

- a card number (Luhn, a real issuer prefix and length) or a card's security code is refused, and so is a password
  given as one; nothing runs and nothing is kept, and the person reads a fixed sentence;
- what only looks like one is left alone: order and tracking numbers, phone numbers, IBANs, ISBNs, an Indian postal
  PIN, a sentence *about* a password;
- a key or a token is masked, on every message (SW1 masked only pasted Agents SDK code);
- an answer is read with its question: a lone secret-looking word in reply to "what's the password?" is refused,
  and one in reply to "paste your API key" is masked;
- the panel's notice carries the message as kept, which the panel shows in place of what the person typed.

Every card number and key below is assembled at run time: a literal one in a source file trips secret scanners.
"""
from __future__ import annotations

import pytest

from core.native_builder.door import (
    CREDENTIAL_MARK,
    MASKED_NOTICE,
    NOT_KEPT,
    REFUSALS,
    Refused,
    check,
    check_answer,
    has_card,
    has_password,
    kept_answer,
    mask_history,
    notice,
)

VISA = "4242" * 4               # Stripe's public test Visa
MASTERCARD = "5555" * 3 + "4444"
AMEX = "3782" + "822463" + "10005"
DISCOVER = "6011" + "1111" * 2 + "1117"
JCB = "3530" + "1113" + "3330" + "0000"


def _spaced(digits: str, sep: str = " ") -> str:
    return sep.join(digits[i:i + 4] for i in range(0, len(digits), 4))


# ── cards ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    f"Pay this invoice with our card {_spaced(VISA)} exp 12/28 cvc 123",
    f"card {VISA}",
    f"use {_spaced(VISA, '-')} please",
    f"Mastercard {MASTERCARD}",
    f"amex: {AMEX}",
    f"Discover {DISCOVER}",
    f"jcb {JCB}",
    "my cvc is 123",
    "CVV: 4321",
    "the card pin 1234",
    "pin is 4471",
])
def test_a_card_number_or_its_code_is_refused(text):
    checked = check(text)
    assert checked.refused == "card"
    assert checked.text == "", "a refused message is not kept, not even masked"
    assert checked.notice == REFUSALS["card"]
    assert checked.event() == {"type": "builder_notice", "reason": "card", "message": REFUSALS["card"],
                               "final": True, "kept": NOT_KEPT}


@pytest.mark.parametrize("text", [
    "order 1234567890123 shipped",
    "tracking 9400111202555842332669",
    "call +1 415 555 0142 or 4155550142",
    "invoice INV-2026-000123",
    "the run took 1791898662000 ms",
    "IBAN GB82 WEST 1234 5698 7654 32",
    "ISBN 978-3-16-148410-0",
    "ship it to PIN 560001",
    "pin code 600001, Chennai",
    f"almost a card {VISA[:-1]}1",          # fails Luhn
    "1234 5678 9012 3456",                   # no issuer starts with 1
    "run_01a136be78a272d285b86d8080fe60d5",
    "We have 4242 patients and 15 chairs",
])
def test_what_only_looks_like_a_card_is_left_alone(text):
    assert not has_card(text), text
    assert check(text).refused is None


# ── passwords ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "my password is hunter2",
    "password: Hunter2-CANARY-portal",
    "pwd=Secret123",
    "passcode: 4471",
    "the password for the insurance portal is Sunny#2026",
    "Password - Tr0ub4dor&3",
])
def test_a_password_given_as_one_is_refused(text):
    checked = check(text)
    assert checked.refused == "password", text
    assert checked.text == ""
    assert checked.notice == REFUSALS["password"]


@pytest.mark.parametrize("text", [
    "reset my password",
    "the password is required",
    "password policy is strict",
    "I forgot my password, how do I change it?",
    "password: <your password>",
    "password: ****",
    "Where do I change the password?",
    "passwords are stored encrypted",
    "the password is set in settings",
    "Log into our insurance portal with my password and download the claims every day.",
])
def test_a_sentence_about_a_password_is_left_alone(text):
    assert not has_password(text), text
    assert check(text).refused is None


# ── keys ─────────────────────────────────────────────────────────────────────

def test_a_pasted_key_is_masked_on_an_ordinary_message_and_the_rest_still_runs():
    stripe = "sk_" + "test_" + "Q" * 24
    github = "ghp_" + "A1b2" * 9
    checked = check(f"Here's my Stripe key {stripe} and GitHub token {github}, connect both")
    assert checked.refused is None
    assert stripe not in checked.text and github not in checked.text
    assert checked.text.count(CREDENTIAL_MARK) == 2
    assert "connect both" in checked.text
    assert checked.removed == 2
    assert checked.event() == {"type": "builder_notice", "reason": "masked", "message": MASKED_NOTICE,
                               "final": False, "kept": checked.text}


def test_any_providers_live_or_test_key_is_masked():
    """Shaped like Stripe's, from any provider (F576): BWA1's after pass saw a Shippo test key reach a run."""
    shippo = "shippo_" + "test_" + "4f9c2a" * 7
    other = "acme_" + "live_" + "Zq81" * 5
    checked = check(f"We'll use the {shippo} credentials, and {other} for the other one")
    assert shippo not in checked.text and other not in checked.text and checked.removed == 2


@pytest.mark.parametrize("text", [
    "rename user_test_integrationsuite to user_test_smoke",   # snake_case names: no digits in the tail
    "the run_live_dashboardpanel view",
    "order 1Z999AA10123456784 is late",                        # a tracking number has no _live_/_test_
])
def test_names_that_only_look_like_a_providers_key_are_left_alone(text):
    assert check(text).removed == 0


def test_an_ordinary_message_passes_unchanged_with_nothing_to_say():
    checked = check("What's the last schedule that ran?")
    assert (checked.text, checked.refused, checked.removed, checked.event()) == (
        "What's the last schedule that ran?", None, 0, None)


# ── answers ──────────────────────────────────────────────────────────────────

def test_an_answer_with_a_card_or_a_password_is_refused():
    with pytest.raises(Refused) as card:
        check_answer({"feedback": f"approve, and charge {VISA}"})
    assert card.value.reason == "card" and str(card.value) == REFUSALS["card"]
    with pytest.raises(Refused) as pwd:
        check_answer({"answer": "my password is hunter2"})
    assert pwd.value.reason == "password"


def test_a_lone_secret_in_reply_to_a_password_question_is_refused():
    with pytest.raises(Refused):
        check_answer({"answer": "Hunter2-CANARY-portal"}, question="What's the insurance portal's password?")


def test_a_lone_secret_in_reply_to_a_key_question_is_masked():
    custom = "dbk_live_" + "X" * 6 + "_9z"
    answer, removed = check_answer({"answer": custom}, question="Please paste your API key here.")
    assert answer == {"answer": CREDENTIAL_MARK} and removed == 1


def test_a_lone_secret_in_reply_to_an_authentication_question_is_masked():
    """The builder asked about authentication, not "a key", and the reply was the key (BWA1's chg-09, F576)."""
    custom = "xr" + "-" + "9Kq2v7" + "-" + "Lm4tP0"
    answer, removed = check_answer({"answer": custom}, question="Can you confirm the Xero authentication details?")
    assert answer == {"answer": CREDENTIAL_MARK} and removed == 1


@pytest.mark.parametrize("reply", ["approve", "yes", "https://api.dentalbook.example/v2", "owner@harbordental.example",
                                   "Harbor chairs"])
def test_an_ordinary_reply_to_a_secret_question_is_left_alone(reply):
    answer, removed = check_answer({"answer": reply}, question="What's your API key?")
    assert answer == {"answer": reply} and removed == 0


@pytest.mark.parametrize("answer,kept", [
    ({"answer": "use " + CREDENTIAL_MARK}, "use " + CREDENTIAL_MARK),
    ("plain " + CREDENTIAL_MARK, "plain " + CREDENTIAL_MARK),
    ({"answer": "a", "more": "b"}, ""),
    ({"answer": ["a"]}, ""),
])
def test_the_panel_is_shown_an_answer_as_kept_only_when_it_is_one_string(answer, kept):
    assert kept_answer(answer) == kept
    event = notice("masked", kept_answer(answer), final=False)
    assert event.get("kept", "") == kept, "an answer that is not one string leaves the panel's bubble alone"


def test_history_drops_a_refused_turn_and_masks_keys():
    stripe = "sk_" + "live_" + "R" * 24
    out = mask_history([
        {"role": "user", "content": f"card {VISA}"},
        {"role": "user", "content": f"key {stripe}"},
        {"role": "assistant", "content": "ok"},
    ])
    assert [t["content"] for t in out] == [f"key {CREDENTIAL_MARK}", "ok"]
