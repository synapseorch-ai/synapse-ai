"""A one-off "read this page and tell me" is read and answered here, in the chat (F1687).

Asked to read a page it names and summarise or compare it, Synapse AI said it could not read a page in the chat and
offered to build a workflow, run it, and send the result somewhere: a one-off read became a build and a destination
question. Now an ask that names its pages by address, to be read and answered once, goes to a step that reads them
and answers in the chat. That step holds the builder's own page reader and nothing else, so nothing in it can change
anything, and a page's words are what it read, never instructions to it.

Near misses, as before: an ask to repeat it ("every Monday"), to send it somewhere, or to build it goes to the build;
a live fact with no page named keeps the offer to build what gets it.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import core.native_builder as nb
from core.native_builder.classify import override
from core.native_builder.objects import Request

NATIVE = Path(nb.__file__).parent
S1 = ("Read https://docs.python.org/3/library/json.html and give me a five-line summary of what the module does and "
      "its main functions.")
S7 = ("Read my GitHub settings page at https://github.com/settings/profile and the page "
      "https://en.wikipedia.org/wiki/Special:Random and summarise both.")


def _route(message: str, kind: str = "answer", **request):
    return override(Request(kind=kind, needs="none", **request), message=message,
                    state={"may_change": True, "may_act": True}, inventory={}, connectable=set())


@pytest.mark.parametrize("message", [S1, S7, "Compare https://docusaurus.io/docs and https://gitbook.com/docs in a "
                                              "short table."])
@pytest.mark.parametrize("kind", ["answer", "build"])
def test_a_one_off_read_of_named_pages_is_answered_here(message, kind):
    routed = _route(message, kind, live_fact="what the page says")
    assert (routed.kind, routed.needs, routed.route) == ("answer", "pages", "step_answer"), routed


@pytest.mark.parametrize("message", [
    "Every Monday, check https://www.allbirds.com/products/mens-tree-runners and tell me if the price changed.",
    "Summarise https://docs.python.org/3/library/json.html and email it to me.",
    "Build a workflow that reads https://docs.python.org/3/library/json.html and summarises it.",
])
def test_an_ask_to_repeat_send_or_build_it_is_never_read_here(message):
    assert _route(message, "build").needs != "pages"


def test_a_live_fact_with_no_page_named_keeps_its_offer():
    routed = _route("What is the euro to dollar rate right now?", live_fact="the euro to dollar rate")
    assert routed.needs == "none" and routed.overridden_by == "live_fact", routed


def test_the_answer_goes_to_the_page_reading_step():
    orch = json.loads((NATIVE / "orchestration.json").read_text(encoding="utf-8"))
    steps = {s["id"]: s for s in orch["steps"]}
    assert steps["step_answer_route"]["switch_cases"].get("pages") == "step_answer_pages"
    reader = steps["step_answer_pages"]
    assert reader["type"] == "agent" and reader["next_step_id"] == "step_answer_checked"
    assert "{{ state.answer_pack.pack }}" in reader["prompt_template"]


def _reader() -> dict:
    agent = json.loads((NATIVE / "agents" / "page_reader.json").read_text(encoding="utf-8"))
    return agent if isinstance(agent, dict) else agent[0]


def test_the_page_reader_holds_the_builders_own_page_reader_and_nothing_else():
    """A builder tool, as every builder agent's are: none of them runs anything, so nothing pasted can be run."""
    reader = _reader()
    assert reader["tools"] == ["read_page"] and reader["skip_default_tools"] is True, reader


def test_a_pages_words_are_what_it_read_never_instructions():
    prompt = _reader()["system_prompt"]
    assert "You are Synapse AI" in prompt and re.search(r"never (?:an )?instructions?", prompt, re.I), prompt[:400]
    assert not re.search(r"\b(Reporter|Talker|Planner|Page Reader)\b", prompt), "it names no part of how it is built"


def test_the_pack_names_the_pages_to_read():
    from core.native_builder.answer_pack import build_pack
    from core.product_tools.roles import Role

    pack = build_pack(routed={"needs": "pages"}, workspace_name="Harbor & Pine", profile="", asker_name="",
                      asker_role=Role.OWNER, last="", capabilities="", help_hits=[], named=[],
                      pages=["https://example.com/a"])
    assert "https://example.com/a" in pack and "read" in pack.lower(), pack
