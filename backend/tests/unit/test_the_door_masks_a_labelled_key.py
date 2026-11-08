"""A key the person labels as one is masked at the builder's door, whatever its shape (F896, SAI2, D300).

Found by SAI2-P2's after-run on the self-hosted scratch (chg-09, Claude Code Haiku, 2026-10-22): asked "what API
endpoint and authentication credential should the tool use?", the person answered "We use Xero. The invoices
endpoint is https://… and the API key is XERO-CANARY-…". The door masks keys by the shapes it knows (`sk-…`,
`ghp_…`, a quoted literal in code) and reads a *lone* word in reply to a key question as one; a key of a vendor's own
shape, said in a sentence, matched neither, so it reached the run's state and its logs.

The class: a value right after a credential label ("API key", "token", "secret", "client secret", "access token",
"key" … then "is", ":", "=" or "-") is a credential however it looks, if it reads like one (letters with digits, not
a URL, not a reference such as `{{ global.X }}`, not a plain word). Sentences *about* keys are left alone.

Every key below is assembled at run time: a literal one in a source file trips secret scanners.
"""
from __future__ import annotations

import pytest

from core.native_builder.door import CREDENTIAL_MARK, MASKED_NOTICE, check, check_answer
from core.secret_scrub import holds_credential, scrub_credentials

CANARY = "XERO" + "-CANARY-" + "7f3a91"
TOKEN = "zq8" + "Lk2" + "Vt7" + "Pm4" + "Rx"


def test_the_found_case_is_masked_and_the_rest_of_the_answer_still_runs():
    """chg-09's answer, word for word but for the key."""
    answer = ("We use Xero. The invoices endpoint is https://api.xero.com/api.xro/2.0/Invoices "
              f"and the API key is {CANARY}.")
    kept, removed = check_answer({"answer": answer}, question=(
        "What is your actual accounting system, and what API endpoint and authentication credential "
        "(API key, token, etc.) should the `fetch_invoices` tool use?"))
    assert CANARY not in kept["answer"]
    assert CREDENTIAL_MARK in kept["answer"]
    assert removed == 1
    assert "We use Xero." in kept["answer"]
    assert "https://api.xero.com/api.xro/2.0/Invoices" in kept["answer"], "the endpoint is not a secret"


@pytest.mark.parametrize("label", [
    "the API key is", "api key:", "API_KEY =", "apikey -", "my token is", "access token:", "auth token =",
    "client secret is", "the secret is", "secret key:", "our key is", "key =",
])
def test_a_value_after_a_credential_label_is_masked_on_any_message(label):
    checked = check(f"Use the Acme API, {label} {TOKEN} please.")
    assert TOKEN not in checked.text
    assert CREDENTIAL_MARK in checked.text
    assert checked.removed == 1 and checked.notice == MASKED_NOTICE
    assert checked.text.endswith("please.")


@pytest.mark.parametrize("text", [
    "the API key is set",
    "the token is expired, can you check?",
    "the API key is stored under Settings, Provider keys",
    "the key is in the vault",
    "the secret is out: we're launching Monday",
    "the key is user_id in that table",
    "the endpoint is https://api.example.com/v2/items and the key is on its settings screen",
    "token: {{ global.ACME_TOKEN }}",
    "key = ${process.env.ACME_KEY}",
    "api key: <your-key-here>",
    "the key is 2026-10-23",
    "the token is gpt-4o",
    "keyboard is broken",
])
def test_a_sentence_about_a_key_or_a_reference_is_left_alone(text):
    assert check(text).text == text
    assert not holds_credential(text)


def test_a_card_on_a_board_or_a_pasted_export_is_scrubbed_by_the_same_rule():
    """`secret_scrub` serves the door, boards' card text and SW1's export alike: one rule for all three."""
    text, removed = scrub_credentials(f"Connect it with the API key is {CANARY} for now")
    assert CANARY not in text and removed == 1
    assert holds_credential(f"token: {TOKEN}")
