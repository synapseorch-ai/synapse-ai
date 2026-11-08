"""
One module says both what holds a secret and what asks for one (`core.secret_scrub`; F1093 with SAI2-P4's F1092).

H45's res-05 (P5's measure, 10-27): Synapse AI's build planner wrote, as the plan card's open question, "you'll need
to provide your Stripe Secret API Key (starts with sk_test_ or sk_live_) and your SendGrid API Key (starts with SG.)".
A key typed in chat is kept with the chat (chg-09); a connection comes through a Connect card, a key through
Settings → Vault. A question naming a secret asks for it (`strict`); prose that describes a workflow is held only to
a sentence that asks for one, so "reminds the team to rotate API keys" can still be said.
"""
from __future__ import annotations

import pytest

from core.secret_scrub import (
    CREDENTIAL_MARK, NEVER_IN_CHAT, asks_for_a_secret, holds_credential, scrub_credentials, without_secret_asks,
)

#: res-05's open question, as the card showed it.
RES_05 = ("To complete this workflow, you'll need to provide your Stripe Secret API Key (starts with sk_test_ or "
          "sk_live_) and your SendGrid API Key (starts with SG.). These will be securely connected to your workspace. "
          "Do you have both keys ready? The system will guide you through adding them after you approve this plan.")
#: Its second round's, after the person said they had none.
RES_05_AGAIN = ("Once you have your Stripe Secret API Key (starts with sk_test_ or sk_live_) and SendGrid API Key "
                "(starts with SG.), you can connect them under Apps in settings, and I'll update this workflow to "
                "automate the payment link creation and email sending.")


class TestAsksForASecret:
    @pytest.mark.parametrize("text", [RES_05, RES_05_AGAIN])
    def test_res_05s_own_words_ask_for_one(self, text):
        assert asks_for_a_secret(text)
        assert asks_for_a_secret(text, strict=False)

    @pytest.mark.parametrize("text", [
        "Please paste your access token here.",
        "Could you share the password for the shared inbox?",
        "I'll need your Shopify API key to continue.",
        "Ask the user for their password before resetting it.",
    ])
    def test_prose_that_asks_for_one(self, text):
        assert asks_for_a_secret(text, strict=False)

    @pytest.mark.parametrize("text", [
        "Which Stripe account should it charge?",
        "Which list should it add them to?",
        "How much should the deposit be?",
    ])
    def test_a_question_only_the_person_can_answer_is_no_ask(self, text):
        assert not asks_for_a_secret(text)
        assert not asks_for_a_secret(text, strict=False)

    @pytest.mark.parametrize("text", [
        "Reminds the team to rotate API keys every quarter.",
        "Calls the Stripe API; its key is kept in the Vault.",
        "Sends the weekly token usage report.",
        "Resets a customer's password on request and emails them a link.",
    ])
    def test_prose_that_describes_a_workflow_asks_for_nothing(self, text):
        assert not asks_for_a_secret(text, strict=False)

    def test_nothing_asks_nothing(self):
        assert not asks_for_a_secret("") and not asks_for_a_secret(None)  # type: ignore[arg-type]


class TestWithoutSecretAsks:
    def test_the_asking_sentence_goes_and_codes_line_is_said_once(self):
        said = f"I built the workflow.\n\n{RES_05_AGAIN} It runs every morning."
        cleaned, was = without_secret_asks(said)
        assert was
        assert "Secret API Key" not in cleaned and "sk_live_" not in cleaned
        assert cleaned.startswith("I built the workflow.") and "It runs every morning." in cleaned
        assert cleaned.count(NEVER_IN_CHAT) == 1 and cleaned.endswith(NEVER_IN_CHAT)

    def test_a_reply_that_only_asked_is_codes_line(self):
        assert without_secret_asks("Please paste your API key here.") == (NEVER_IN_CHAT, True)

    def test_a_reply_that_asks_nothing_is_kept_as_it_was(self):
        said = "Built it.\n\n- Step 1: reads the sheet\n- Step 2: rotates API keys"
        assert without_secret_asks(said) == (said, False)

    def test_with_no_line_only_the_ask_is_cut(self):
        cleaned, was = without_secret_asks(f"Charges the patient. {RES_05_AGAIN}", line=None)
        assert was and cleaned == "Charges the patient."


class TestHoldsASecret:
    """F1092 (SAI2-P4, D300): team res-04's canary passed the door. Its key matched no shape — a `<name>_test_` key
    with a short tail — and "the credential X" has no "is" or ":" after the label. Every key below is assembled at
    run time: a literal one in a source file trips secret scanners."""

    CANARY = "shippo_test_" + "CANARY" + "bwa1"

    @pytest.mark.parametrize("text", [
        "connect the Shippo MCP server with the credential {key}.",
        "I've connected Shippo using the credential {key}. The automation is ready.",
        "buy a Shippo label for each order (using {key}) and return the tracking numbers.",
        "the Shippo key is {key}",
    ])
    def test_res_04s_own_words_are_masked(self, text):
        from core.native_builder.door import check

        checked = check(text.format(key=self.CANARY))
        assert self.CANARY not in checked.text and "CANARY" not in checked.text
        assert CREDENTIAL_MARK in checked.text and checked.removed == 1
        assert checked.refused is None, "a key is masked and the message still runs"

    @pytest.mark.parametrize("label", ["the credential", "my credential", "API key", "api_key", "access token",
                                       "client secret", "secret key", "auth token"])
    def test_a_value_right_after_a_credential_label_is_masked(self, label):
        token = "zq8" + "Lk2" + "Vt7" + "Pm4"
        text, removed = scrub_credentials(f"Use the Acme API with the {label} {token} please.")
        assert token not in text and removed == 1 and text.endswith("please.")

    @pytest.mark.parametrize("key", ["acme_live_" + "Ab3" + "dE9" + "fG", "shippo_test_" + "X7" + "yy" + "Q2" + "pp"])
    def test_a_short_provider_key_mixing_cases_and_digits_is_masked(self, key):
        assert holds_credential(f"here it is: {key}")

    @pytest.mark.parametrize("text", [
        "the tool writes unit_test_results2024 to the sheet",
        "rename it to load_test_runner42",
        "the field is api_test_cases1 in that table",
        "the token expired yesterday",
        "the API key stored under Settings",
        "a credential like that goes in the Vault",
        "use the access token from the vault",
        "the key user_id1 joins the tables",
        "my credential manager2 holds it",
    ])
    def test_names_and_sentences_about_keys_are_not_masked(self, text):
        assert not holds_credential(text), text
        assert scrub_credentials(text) == (text, 0)
