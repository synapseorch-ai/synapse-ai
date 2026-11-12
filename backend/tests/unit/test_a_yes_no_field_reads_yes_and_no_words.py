"""A Yes/No field takes the words a person says yes and no with — F1552 (BLD3; Naveen's pick 11-12, after F1549).

F1549 has the builder ask a sign-off a later step branches on as a Yes/No field, `approved`. The run page sends true or
false; a channel's reply or an API caller sends words, and F1434 read only yes, y, true, 1 and no, n, false, 0 as the
flag. Any other word was kept as it came, so "decline" sent through the API stayed the text "decline", and a plain
`if state.review.approved` read it as true: a declined sign-off went on.

Now every Yes/No field reads approve, approved, accept, accepted as yes and decline, declined, reject, rejected as no,
beside F1434's words, trimmed, in any case, a closing full stop or bang dropped: in a mapping keyed by the field, and as
a channel's plain reply to a step whose one field is a yes/no. Anything else is still kept as it came, as F1434 keeps
it: nothing answered is refused. A tool call's own approval (`engine._approved`) reads its words as before.
"""
from __future__ import annotations

import pytest

from core.orchestration.human_fields import as_flag, check_answer

FIELD = {"name": "approved", "type": "boolean", "label": "Approve these keywords?"}
NEW_YES = ["approve", "approved", "accept", "accepted"]
NEW_NO = ["decline", "declined", "reject", "rejected"]


class TestTheWords:
    @pytest.mark.parametrize("said", NEW_YES + ["yes", "y", "true", "1"])
    def test_yes(self, said):
        for written in (said, said.upper(), said.title(), f"  {said} ", f"{said}.", f"{said}!"):
            assert as_flag(written) is True, written
            assert check_answer([FIELD], {"approved": written}) == {"approved": True}, written

    @pytest.mark.parametrize("said", NEW_NO + ["no", "n", "false", "0"])
    def test_no(self, said):
        for written in (said, said.upper(), said.title(), f"  {said} ", f"{said}.", f"{said}!"):
            assert as_flag(written) is False, written
            assert check_answer([FIELD], {"approved": written}) == {"approved": False}, written

    @pytest.mark.parametrize("said, flag", [("Approve", True), ("decline.", False), ("Rejected", False),
                                            ("ACCEPT", True)])
    def test_a_channels_plain_reply_to_a_lone_yes_no(self, said, flag):
        assert check_answer([FIELD], said) == {"approved": flag}

    @pytest.mark.parametrize("said", ["maybe", "approve it later", "acceptable", "declined to say", "approval",
                                      "not approved", "", None, "on", "off"])
    def test_anything_else_is_kept_as_it_came(self, said):
        assert check_answer([FIELD], {"approved": said}) == {"approved": said}

    def test_a_tool_calls_own_approval_reads_as_before(self):
        from core.orchestration.engine import OrchestrationEngine

        assert OrchestrationEngine._approved("approve")[0] is True
        assert OrchestrationEngine._approved("accept")[0] is False
        assert OrchestrationEngine._approved("decline")[0] is False


class TestASignOffRoutesOnIt:
    """The sign-off F1549's builder makes, with a plain `if approved`, answered through the doors' own path."""

    @pytest.mark.parametrize("answer, arm", [({"approved": "decline"}, "false"), ({"approved": "Rejected."}, "false"),
                                             ({"approved": "approve"}, "true"), ("accepted", "true"),
                                             ("declined", "false")])
    async def test_a_word_through_the_api_or_a_channel(self, answer, arm):
        from _fakes import seed as S
        from test_a_condition_reads_a_field_its_step_writes import _answered, _ask, _gate, _print

        name = "".join(c for c in str(answer) if c.isalnum()).lower()
        orch = S.make_orchestration(id=f"orch_f1552_{name}", entry_step_id="approve_keywords", steps=[
            {**_ask([FIELD]), "next_step_id": "check_approval"},
            {**_gate("state.approve_keywords.approved"), "if_true_step_id": "yes", "if_false_step_id": "no"},
            _print("yes"), _print("no"),
        ])
        assert await _answered(orch, answer) == arm
