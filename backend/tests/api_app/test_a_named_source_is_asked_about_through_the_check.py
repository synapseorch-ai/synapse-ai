"""Through the builder's own check, a source the person named gets their pick of a base here (F1763).

With Knowledge on and a "Help centre" base here, a plan whose agent drafts "from our office plans" and is given no
base is asked about on the form before any plan is shown; the answer "Help centre" puts that base on the agent, used
and never made, and "None" builds it as it was.
"""
from __future__ import annotations

import pytest

from _fakes.knowledge_db import knowledge_store  # noqa: F401 — fixture
from core.knowledge import seams
from core.knowledge.testing.fakes import RecordingDispatcher
from core.native_builder import build_plan as B

from tests.unit.test_a_build_is_saved_in_code import _plan, _reviewed, workspace  # noqa: F401
from tests.unit.test_a_named_source_gets_a_base_or_a_question import AGENT, LABEL, MESSAGE

pytestmark = pytest.mark.usefixtures("knowledge_store")

DRAFT_STEP = {"id": "claim", "type": "agent", "name": "Claim & Draft", "agent": AGENT,
              "does": "Drafts a short warm reply to the enquiry from the office plans",
              "prompt": "Draft a reply to {{ state.input }}.", "next": "end"}
DESK = {"name": AGENT, "purpose": "Drafts replies to office enquiries", "tools": []}


@pytest.fixture
async def help_centre(client):
    assert (await client.post("/api/settings", json={"knowledge_enabled": True})).status_code == 200
    seams.set_ingest_dispatcher(RecordingDispatcher())
    return (await client.post("/api/knowledge/bases", json={"name": "Help centre", "folder": "help"})).json()


def _answered(answer: str) -> dict:
    from core.native_builder.change_tools import ASKED_KEY

    field = {"name": "q1", "type": "choice", "label": LABEL, "options": ["Help centre", B.NO_BASE]}
    return {"user_message": MESSAGE, B.CHECKED_KEY: {"outcome": "ask", "form": [field]},
            "human_response": {"form": {"q1": answer}},
            ASKED_KEY: [{"about": "which", "subject": "source from our office plans", "question": LABEL,
                         "field": "q1"}]}


def _draft() -> dict:
    return _plan(DRAFT_STEP, agents=[DESK], title="Sales desk", summary="Drafts replies to office enquiries.")


async def test_the_question_comes_before_any_plan(help_centre):
    _run, checked = await _reviewed(_draft(), user_message=MESSAGE)
    assert checked["outcome"] == "ask", checked.get("outcome")
    [field] = checked["form"]
    assert field["label"] == LABEL and field["options"] == ["Help centre", B.NO_BASE]


async def test_a_pick_puts_the_base_here_on_the_agent(help_centre):
    _run, checked = await _reviewed(_draft(), prompts={AGENT: "You draft replies."}, **_answered("Help centre"))
    plan = checked["plan"]
    assert [(k["name"], k["existing"]) for k in plan["knowledge"]] == [("Help centre", True)], plan["knowledge"]
    assert plan["agents"][0]["knowledge"] == ["Help centre"]


async def test_none_builds_it_as_it_was(help_centre):
    _run, checked = await _reviewed(_draft(), prompts={AGENT: "You draft replies."}, **_answered(B.NO_BASE))
    plan = checked["plan"]
    assert plan["knowledge"] == [] and not plan["agents"][0].get("knowledge")
