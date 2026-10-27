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

from core.secret_scrub import NEVER_IN_CHAT, asks_for_a_secret, without_secret_asks

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
