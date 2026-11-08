"""A key named by its header, or typed while its card is pending, is masked (F1387, TRI1; eb 11-05; Naveen 11-05:
(a) + (c), "Build as restated").

(a) A value after a bare "key", with no "is", ":" or "=", is a key when the same sentence names a key's header (an
    `…-Api-Key`, `Authorization`, an `X-…` header named for a key, or a key-named header said as a header), and it
    reads like one: 12 or more characters mixing letters and digits. "the key user_id1" stays a sentence (F1092).
(c) At a builder run's wait whose plan asks for a key on a card after the yes, the answer is read as if that card were
    up (F1345's rule): a lone key-like answer is masked, and so is a value after a bare "key" anywhere in it. A
    key-like word with no "key" before it is not: a model's name looks the same.

Every key is assembled at run time; a lookalike by its code point.
"""
from __future__ import annotations

import json

import pytest

from core.secret_scrub import CREDENTIAL_MARK, holds_credential, scrub_credentials

NB = chr(0x2011)
VALUE = "-".join(["amber", "fox", "river", "4821"])
PENDING = {"plan_checked": json.dumps({"plan": {"tools": [
    {"name": "orders_api_getorderstatus", "status": "found", "via": "http_tool",
     "api": {"auth": {"name": "X-Api-Key", "scheme": ""}}}]}})}


class TestNamedByItsHeader:
    @pytest.mark.parametrize("text, masked", [
        (f"Use the X-Api-Key header (key {VALUE}).", f"Use the X-Api-Key header (key {CREDENTIAL_MARK})."),
        (f"Send key {VALUE} in the Authorization header", f"Send key {CREDENTIAL_MARK} in the Authorization header"),
        (f"the api_key header takes key {VALUE}", f"the api_key header takes key {CREDENTIAL_MARK}"),
        (f"its x-auth-token is sent with key {VALUE}", f"its x-auth-token is sent with key {CREDENTIAL_MARK}"),
        ("Set the Orders-Token header with key ORDERS2026REVIEW today",
         f"Set the Orders-Token header with key {CREDENTIAL_MARK} today"),
    ])
    def test_a_value_after_a_bare_key(self, text, masked):
        assert scrub_credentials(text)[0] == masked and holds_credential(text), text

    def test_written_with_lookalikes_as_pasted(self):
        header, value = NB.join(["X", "Api", "Key"]), NB.join(["amber", "fox", "river", "4821"])
        text = f"the {header} header (key{chr(0x202F)}{value})"
        assert scrub_credentials(text)[0] == f"the {header} header (key{chr(0x202F)}{CREDENTIAL_MARK})"

    def test_the_door_masks_it(self):
        from core.native_builder.door import check

        assert check(f"Use the X-Api-Key header (key {VALUE}).").text == \
            f"Use the X-Api-Key header (key {CREDENTIAL_MARK})."

    @pytest.mark.parametrize("text", [
        "the key user_id1",
        "the key user_id1 goes in the X-Api-Key header",
        f"The key {VALUE} from the meeting",
        "Set the X-Api-Key header to the key from Vault.",
        "the key 4821 in the X-Api-Key header",
        "Send key amber-fox-river-lake in the Authorization header",
        f"Use the key {VALUE} for the primary_key column",
        f"key {VALUE} and an x-request-id header",
    ])
    def test_a_sentence_is_not_a_key(self, text):
        assert scrub_credentials(text) == (text, 0) and not holds_credential(text), text


class TestWhileItsCardIsPending:
    def test_a_plan_asking_for_a_key_after_the_yes(self):
        from core.native_builder.turn_facts import key_card_pending

        assert key_card_pending(PENDING)
        keyless = {"plan_checked": json.dumps({"plan": {"tools": [
            {"name": "t", "status": "found", "via": "http_tool", "api": {"keyless": True}}]}})}
        here = {"plan_checked": json.dumps({"plan": {"tools": [
            {"name": "t", "status": "present", "via": "http_tool", "api": {"auth": {"name": "X-Api-Key"}}}]}})}
        for state in (keyless, here, {}, {"plan_checked": ""}, None):
            assert not key_card_pending(state), state

    async def test_it_is_read_as_a_card_up(self):
        from core.native_builder.turn_facts import card_up

        assert await card_up("builder_f1387_pending", PENDING) is True
        assert await card_up("builder_f1387_nothing", {}) is False

    @pytest.mark.parametrize("answer", [
        f"Looks good, use key {VALUE} for it",
        f"approve. key {VALUE}",
        VALUE,
    ])
    def test_a_key_in_the_answer_is_masked(self, answer):
        from core.native_builder.door import check_answer

        masked, removed = check_answer({"feedback": answer}, "Approve the plan?", step="step_human_review",
                                       card_up=True)
        assert VALUE not in json.dumps(masked) and CREDENTIAL_MARK in masked["feedback"] and removed, masked

    @pytest.mark.parametrize("answer", [
        "use claude-haiku-4-5 instead",
        "the key user_id1 is fine",
        "Looks good, approve it",
    ])
    def test_no_key_no_change(self, answer):
        from core.native_builder.door import check_answer

        assert check_answer({"feedback": answer}, "Approve the plan?", step="step_human_review",
                            card_up=True) == ({"feedback": answer}, 0)

    def test_with_no_card_a_bare_key_in_a_sentence_is_as_before(self):
        from core.native_builder.door import check_answer

        answer = f"Looks good, use key {VALUE} for it"
        assert check_answer({"feedback": answer}, "Approve the plan?", step="step_human_review") == \
            ({"feedback": answer}, 0)


class TestEveryWayAnAnswerArrives:
    """The builder's resume route and the run answer door read the run's own plan, never words."""

    async def test_the_answer_door_reads_the_runs_plan(self):
        from core.orchestration import answers

        assert await answers._card_up("builder_f1387_pending", PENDING) is True
        assert await answers._card_up("run_a_workflow", PENDING) is False  # a workflow's run is never asked

    async def test_with_no_state_given_it_reads_the_run(self, monkeypatch):
        from core.native_builder import turn_facts

        async def state_of(run_id):
            return PENDING if run_id == "builder_f1387_kept" else {}

        monkeypatch.setattr(turn_facts, "_state_of", state_of)
        assert await turn_facts.card_up("builder_f1387_kept") is True
        assert await turn_facts.card_up("builder_f1387_other") is False
