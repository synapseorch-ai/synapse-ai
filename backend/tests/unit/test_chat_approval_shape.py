"""The parked call and the resume state, as shapes.

Two callers park (the API process and a worker) and two build a resume state
(the API process and the commercial control plane). They have to agree exactly,
because a resume that hands the loop a state it does not recognise re-runs the
tools that already succeeded — which is the one outcome the snapshot exists to
prevent.
"""
from core.chat_approval import parked_payload, resume_state_for

EVENT = {
    "type": "human_input_required",
    "kind": "tool_approval",
    "tool_name": "run_sql_query",
    "tool_args": {"sql": "delete from invoices"},
    "category": "destructive",
    "approval_id": "a1b2c3d4",
}


class TestParking:
    def test_it_carries_the_call_the_question_and_the_snapshot(self):
        parked = parked_payload(EVENT, message="tidy the invoices", progress={"turn": 3})
        assert parked == {
            "tool_name": "run_sql_query",
            "tool_args": {"sql": "delete from invoices"},
            "approval_id": "a1b2c3d4",
            "category": "destructive",
            "message": "tidy the invoices",
            "progress": {"turn": 3},
        }

    def test_a_first_turn_pause_has_no_snapshot_and_that_is_fine(self):
        """A turn can stop to ask before it has reached a turn boundary."""
        parked = parked_payload(EVENT, message="", progress=None)
        assert parked["progress"] == {}
        assert parked["message"] == ""


class TestResumeState:
    def test_approving_records_the_call_and_keeps_the_snapshot(self):
        parked = parked_payload(EVENT, message="go", progress={"turn": 3, "tools_used": ["ls"]})
        state = resume_state_for(parked, approve=True)

        assert state["turn"] == 3
        assert state["tools_used"] == ["ls"]
        assert state["approved_calls"] == ["a1b2c3d4"]
        assert state["pending_call"] == {
            "approval_id": "a1b2c3d4",
            "tool": "run_sql_query",
            "arguments": {"sql": "delete from invoices"},
            "approved": True,
            "reason": "",
        }

    def test_an_approval_joins_the_ones_already_given(self):
        parked = parked_payload(EVENT, message="go", progress={"approved_calls": ["older"]})
        state = resume_state_for(parked, approve=True)
        assert state["approved_calls"] == ["a1b2c3d4", "older"]

    def test_refusing_writes_the_other_bucket_and_carries_the_reason(self):
        parked = parked_payload(EVENT, message="go", progress={})
        state = resume_state_for(parked, approve=False, reason="not on production")

        assert state["denied_calls"] == ["a1b2c3d4"]
        assert "approved_calls" not in state
        assert state["denied_reasons"] == {"a1b2c3d4": "not on production"}
        assert state["pending_call"]["approved"] is False

    def test_refusing_without_a_reason_adds_no_empty_reason(self):
        """An empty string in `denied_reasons` reads to the model as a reason
        that was given and was blank."""
        parked = parked_payload(EVENT, message="go", progress={})
        state = resume_state_for(parked, approve=False)
        assert "denied_reasons" not in state

    def test_the_snapshot_is_copied_not_shared(self):
        """The parked row outlives the resume; mutating one must not touch it."""
        progress = {"turn": 3, "approved_calls": ["older"]}
        parked = parked_payload(EVENT, message="go", progress=progress)
        state = resume_state_for(parked, approve=True)
        state["turn"] = 99
        assert progress["turn"] == 3
        assert parked["progress"]["approved_calls"] == ["older"]
