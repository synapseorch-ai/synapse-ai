"""The person's pick on the empty-base question is the plan's, and keeping the new base is said on the card (F1742).

Through the builder's own check, with Knowledge on and a "Help centre" base here: the answer to F1742's choice comes
back as F1302's form answers do. "Help centre" swaps the plan to it (used, never made); keeping "Office plans" builds
it and the card says it starts empty and where documents are added.
"""
from __future__ import annotations

import pytest

from _fakes.knowledge_db import knowledge_store  # noqa: F401 — fixture
from core.knowledge import seams
from core.knowledge.testing.fakes import RecordingDispatcher
from core.native_builder import build_plan as B

from tests.unit.test_a_build_is_saved_in_code import _plan, _reviewed, workspace  # noqa: F401
from tests.unit.test_a_new_empty_base_is_said_and_one_here_offered import KEEP, LABEL

pytestmark = pytest.mark.usefixtures("knowledge_store")

FIND = {"id": "find", "type": "retrieve", "name": "Find the plan", "does": "Searches the office plans",
        "config": {"retrieve": {"kb_ids": ["Office plans"], "query": "{{ state.input }}"}}, "next": "answer"}
ANSWER = {"id": "answer", "type": "agent", "name": "Answer", "does": "Answers the enquiry from what it found",
          "agent": "Sales desk", "prompt": "Answer {{ state.input }} from {{ state.find }}.", "next": "end"}
DESK = {"name": "Sales desk", "purpose": "Answers sales enquiries", "knowledge": ["Office plans"], "tools": []}
EMPTY = "“Office plans” is new and empty: its agents can't answer from it until you add documents to it on the " \
        "Knowledge screen."


@pytest.fixture
async def help_centre(client):
    assert (await client.post("/api/settings", json={"knowledge_enabled": True})).status_code == 200
    seams.set_ingest_dispatcher(RecordingDispatcher())
    return (await client.post("/api/knowledge/bases", json={"name": "Help centre", "folder": "help"})).json()


def _answered(answer: str) -> dict:
    """The run as F1742's question leaves it: the form on the last check, the question kept as asked (as
    `note_question` keeps it), and the person's answer to it."""
    from core.native_builder.change_tools import ASKED_KEY

    field = {"name": "q1", "type": "choice", "label": LABEL, "options": ["Help centre", KEEP]}
    return {B.CHECKED_KEY: {"outcome": "ask", "form": [field]}, "human_response": {"form": {"q1": answer}},
            ASKED_KEY: [{"about": "which", "subject": "empty knowledge base Office plans", "question": LABEL,
                         "field": "q1"}]}


def _draft() -> dict:
    return _plan(FIND, ANSWER, agents=[DESK], knowledge=[{"name": "Office plans", "purpose": "Our office plans"}])


async def test_a_pick_swaps_the_plan_to_the_base_here(help_centre):
    run, checked = await _reviewed(_draft(), prompts={"Sales desk": "You answer sales enquiries."},
                                   **_answered("Help centre"))
    plan = checked["plan"]
    assert [(k["name"], k["existing"]) for k in plan["knowledge"]] == [("Help centre", True)], plan["knowledge"]
    assert plan["agents"][0]["knowledge"] == ["Help centre"]
    assert "build" in checked["offers"], checked.get("issues")
    assert EMPTY not in " ".join(checked.get("notes") or [])


async def test_keeping_it_builds_it_and_the_card_says_it_starts_empty(help_centre):
    run, checked = await _reviewed(_draft(), prompts={"Sales desk": "You answer sales enquiries."},
                                   **_answered(KEEP))
    assert [(k["name"], k["existing"]) for k in checked["plan"]["knowledge"]] == [("Office plans", False)]
    assert "build" in checked["offers"], checked.get("issues")
    assert EMPTY in " ".join(checked["notes"]) and EMPTY in checked["say"]
