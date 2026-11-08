"""A run says what it changed, from its own writes, never from a model's memory of it (F490, BWA1, D274).

The AI builder ended every build and every change with a model's summary, and the summary said what the model
believed: "No workspace changes were made" after creating workflows, agents and tools; "the schedule has been
disabled" with nothing changed; "it successfully books a train ticket" from agents with no tools. These pin the
record that replaces it (`core/orchestration/run_writes.py`):

- each `resource_saved` a step yields is kept in the run's state, and the words follow it;
- the words say what each resource ended as: a workflow built, agents created, a schedule created paused, a
  workflow removed again because it never passed validation — once each, whatever happened in between;
- a run that changed nothing says so, with the last error a builder tool returned;
- an ordinary workflow's tool errors are not this record's business.
"""
from __future__ import annotations

import json

from core.orchestration import run_writes as rw


def _saved(kind, action, id_, name, **resource):
    return {"type": "resource_saved", "kind": kind, "action": action, "id": id_, "name": name,
            "resource": resource or None}


def _run(*events) -> dict:
    state: dict = {}
    for e in events:
        if e["type"] == "resource_saved":
            rw.record(state, e)
        else:
            rw.note_failure(state, e)
    return state


def test_a_build_says_the_workflow_its_agents_and_its_paused_schedule():
    state = _run(
        _saved("agent", "created", "a1", "Reader"),
        _saved("agent", "created", "a2", "Approver"),
        _saved("orchestration", "created", "o1", "Invoice reader"),
        _saved("orchestration", "updated", "o1", "Invoice reader"),
        _saved("orchestration", "updated", "o1", "Invoice reader"),
        _saved("schedule", "created", "s1", "Every Friday", enabled=False),
    )
    assert state[rw.TEXT_KEY] == (
        'Built the workflow "Invoice reader". Created the agents "Reader" and "Approver". '
        'Created the schedule "Every Friday", paused: turn it on under Schedules when you\'re ready.')
    assert len(state[rw.WRITES_KEY]) == 6


def test_a_workflow_removed_again_says_why_and_is_not_said_to_be_built():
    state = _run(
        _saved("agent", "created", "a1", "Tip writer"),
        _saved("orchestration", "created", "o1", "Daily tweet"),
        {"type": "resource_saved", "kind": "orchestration", "action": "deleted", "id": "o1", "name": "Daily tweet"},
    )
    text = state[rw.TEXT_KEY]
    assert 'Built the workflow' not in text
    assert 'Removed the workflow "Daily tweet" again: it never passed validation.' in text
    assert 'Created the agent "Tip writer".' in text


def test_a_change_says_what_it_changed():
    state = _run(_saved("schedule", "updated", "s1", "Every night at 2", enabled=False),
                 _saved("agent", "updated", "a1", "Front desk"))
    assert state[rw.TEXT_KEY] == (
        'Changed the agent "Front desk". Changed the schedule "Every night at 2", paused: turn it on under '
        'Schedules when you\'re ready.')


def test_a_deletion_is_said():
    assert _run(_saved("agent", "deleted", "a1", "Support bot"))[rw.TEXT_KEY] == 'Deleted the agent "Support bot".'


def test_nothing_changed_is_said_with_the_last_builder_error():
    failure = {"type": "tool_result", "tool_name": "set_schedule_enabled",
               "preview": json.dumps({"error": "No schedule named 'Nightly'."})}
    state = _run(failure)
    assert state[rw.TEXT_KEY] == "Nothing in your workspace changed. The last thing that failed: No schedule named 'Nightly'."


def test_an_ordinary_tools_error_is_not_kept():
    state = _run({"type": "tool_result", "tool_name": "scrape_url", "preview": json.dumps({"error": "timeout"})})
    assert state == {}


def test_render_on_nothing_is_nothing_changed():
    assert rw.render([]) == rw.NOTHING_CHANGED and rw.render(None) == rw.NOTHING_CHANGED
