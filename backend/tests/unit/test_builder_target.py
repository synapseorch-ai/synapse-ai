"""Which orchestration a builder message is about.

"Update the second step to get the perfect json on the extended research
orchestration" was sent to the builder five times in one testing round, and the
router answered `create` every time, quoting its own rule back: an update needs
`current_orchestration_id`, and it was empty. Nothing turned a name into an id,
and nothing said which orchestration was open on screen.

The resolver is pure: the caller hands it only the orchestrations the builder
may touch, so a system row, another tenant's row or an unsaved draft id is
simply not in the list.
"""
from __future__ import annotations

from core.native_builder.target import (
    AMBIGUOUS,
    NAMED,
    PICKED,
    TARGET_KEYS,
    VIEWING,
    describe_page,
    resolve_target,
)

ROWS = [
    {"id": "orch_ext", "name": "extended research"},
    {"id": "orch_res", "name": "Research"},
    {"id": "orch_weekly", "name": "Weekly Report"},
]

NOTHING = {key: "" for key in TARGET_KEYS if key != "current_page"}


class TestTheOpenOrchestration:
    def test_it_is_the_target_when_the_message_names_nothing(self):
        target = resolve_target("add a step that emails me the summary", "orch_weekly", ROWS)
        assert (target.id, target.name, target.source) == ("orch_weekly", "Weekly Report", VIEWING)

    def test_an_id_the_builder_may_not_touch_is_dropped(self):
        """The builder's own orchestration, another tenant's, a draft that was
        never saved — none of them is in the caller's list."""
        for raw in ("orch_native_builder", "orch_someone_elses", "orch_temp_draft"):
            assert resolve_target("add a step", raw, ROWS).state() == NOTHING

    def test_naming_the_open_one_is_not_a_conflict(self):
        target = resolve_target("update the extended research orchestration", "orch_ext", ROWS)
        assert (target.id, target.source) == ("orch_ext", NAMED)


class TestANamedOrchestration:
    def test_the_message_from_the_testing_round_resolves(self):
        target = resolve_target(
            "update the second step to get the perfect json on the extended research orchestration",
            None,
            ROWS,
        )
        assert (target.id, target.name, target.source) == ("orch_ext", "extended research", NAMED)

    def test_case_and_punctuation_do_not_matter(self):
        target = resolve_target("Change “Weekly-Report”: send it on Fridays", "", ROWS)
        assert (target.id, target.source) == ("orch_weekly", NAMED)

    def test_a_name_inside_a_longer_name_drops_out(self):
        """"Research" is inside "extended research"; the message named one thing."""
        target = resolve_target("edit extended research", None, ROWS)
        assert target.id == "orch_ext"
        assert target.matches == ()

    def test_part_of_a_word_is_not_a_match(self):
        assert resolve_target("update the researcher agent's prompt", None, ROWS).state() == NOTHING

    def test_an_id_in_the_message_counts(self):
        target = resolve_target("add a retry to orch_weekly please", None, ROWS)
        assert (target.id, target.source) == ("orch_weekly", NAMED)


class TestAnUnclearTarget:
    """Nothing is resolved, on purpose: the builder asks rather than edits."""

    def test_two_names_leave_the_id_empty(self):
        target = resolve_target("copy the research step into weekly report", None, ROWS)
        assert (target.id, target.source) == ("", AMBIGUOUS)
        assert [m.split(" (")[0] for m in target.matches] == ["Research", "Weekly Report"]

    def test_two_orchestrations_with_one_name(self):
        rows = [*ROWS, {"id": "orch_ext_2", "name": "Extended Research"}]
        target = resolve_target("update the extended research orchestration", None, rows)
        assert (target.id, target.source) == ("", AMBIGUOUS)
        assert len(target.matches) == 2

    def test_a_name_that_is_not_the_open_one(self):
        target = resolve_target(
            "update the extended research orchestration to use a loop", "orch_weekly", ROWS
        )
        assert (target.id, target.source) == ("", AMBIGUOUS)
        assert any("open on screen" in m and "Weekly Report" in m for m in target.matches)
        assert any("named in the message" in m and "extended research" in m for m in target.matches)

    def test_the_state_carries_the_matches_as_text(self):
        state = resolve_target("copy research into weekly report", None, ROWS).state()
        assert state["current_orchestration_source"] == AMBIGUOUS
        assert "orch_res" in state["current_orchestration_matches"]
        assert "orch_weekly" in state["current_orchestration_matches"]



TWINS = [*ROWS, {"id": "orch_ext_2", "name": "Extended Research"}]
#: What the previous turn asked about: two orchestrations with one name.
ASKED = resolve_target("update the extended research orchestration", None, TWINS).matches


class TestAReplyToTheQuestion:
    """F16. The builder asked which one; the next message answers it.

    Measured on cloud dev before this: "the second one" reached the router with
    no target and no history, was routed to `answer`, and nothing was changed.
    The reply picks only from the list it answers, and only when it is nothing
    but the choice — otherwise the builder asks again, as D93 decided.
    """

    def test_an_ordinal_picks_from_the_list_it_answers(self):
        assert [m.split(" (")[1].split(")")[0] for m in ASKED] == ["orch_ext", "orch_ext_2"]
        for reply, want in (
            ("the second one", "orch_ext_2"),
            ("2", "orch_ext_2"),
            ("#1", "orch_ext"),
            ("I mean the first", "orch_ext"),
            ("the 2nd please", "orch_ext_2"),
            ("the last one", "orch_ext_2"),
        ):
            target = resolve_target(reply, None, TWINS, previous_matches=ASKED)
            assert (target.id, target.source) == (want, PICKED), reply

    def test_the_stored_text_of_the_list_works_as_well(self):
        text = resolve_target("update the extended research orchestration", None, TWINS).state()[
            "current_orchestration_matches"
        ]
        assert resolve_target("the second one", None, TWINS, previous_matches=text).id == "orch_ext_2"

    def test_a_candidate_named_in_full_is_picked_though_another_is_open(self):
        asked = resolve_target(
            "update the extended research orchestration to use a loop", "orch_weekly", ROWS
        ).matches
        target = resolve_target("extended research", "orch_weekly", ROWS, previous_matches=asked)
        assert (target.id, target.source) == ("orch_ext", PICKED)

    def test_a_message_that_is_more_than_a_choice_is_not_one(self):
        target = resolve_target("add 2 steps to it", None, TWINS, previous_matches=ASKED)
        assert (target.id, target.source) == ("", "")

    def test_an_ordinal_past_the_list_picks_nothing(self):
        assert resolve_target("the third one", None, TWINS, previous_matches=ASKED).id == ""

    def test_a_candidate_deleted_since_is_not_picked(self):
        target = resolve_target("the second one", None, ROWS, previous_matches=ASKED)
        assert (target.id, target.source) == ("", "")

    def test_without_a_question_an_ordinal_means_nothing(self):
        assert resolve_target("the second one", None, TWINS).state() == NOTHING

    def test_the_state_says_it_was_picked(self):
        state = resolve_target("2", None, TWINS, previous_matches=ASKED).state()
        assert state["current_orchestration_source"] == PICKED
        assert state["current_orchestration_name"] == "Extended Research"
        assert state["current_orchestration_matches"] == ""

class TestNothingToGoOn:
    def test_no_page_and_no_name(self):
        assert resolve_target("build me a daily digest", None, ROWS).state() == NOTHING

    def test_an_empty_workspace(self):
        assert resolve_target("update the extended research orchestration", "orch_ext", []).state() == NOTHING


class TestThePageLine:
    def test_it_names_what_is_open(self):
        line = describe_page("orchestration", "extended research", "orch_ext")
        assert "orchestration" in line and "extended research" in line and "orch_ext" in line

    def test_a_resource_keyed_by_name_has_no_empty_id(self):
        assert "()" not in describe_page("skill", "summarise", "")
