"""Synapse AI answering a question speaks as Synapse AI, never names a part of how it is built, and, asked to build,
says how to ask for it (F1689).

The step that answers questions about the workspace was told "You are the Reporter for Synapse AI" and "There is
nothing you can create, change or delete yourself". A build turn the quick model sent to it by mistake was answered with
that role's name and "I cannot build". Its instructions now open as Synapse AI, never name a role, and say that a
change is asked for and a plan drafted for the person to check, never that it can't be done.

Near miss: the talk step, the other one that answers the person directly, names no role either.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import core.native_builder as nb

AGENTS = Path(nb.__file__).parent / "agents"
ROLES = re.compile(r"\b(Reporter|Talker|Planner|Classifier|Maker|Prompt Writer|Researcher)\b")


def _prompt(name: str) -> str:
    """The agent's prompt as it runs: its library passages written in, as the model reads it."""
    from core.native_builder import seed

    agent = json.loads((AGENTS / f"{name}.json").read_text(encoding="utf-8"))
    agent = agent if isinstance(agent, dict) else agent[0]
    return seed.expand_prompts(str(agent.get("system_prompt") or ""))


def test_the_answering_step_speaks_as_synapse_ai_and_names_no_role():
    text = _prompt("reporter")
    assert "You are Synapse AI" in text.split("\n\n", 2)[0] + text.split("\n\n", 2)[1], text[:300]
    assert not ROLES.search(text), ROLES.search(text)
    assert "never name" in text.lower() and "how you are built" in text.lower(), "it is told never to name its parts"


def test_asked_to_build_it_says_how_to_ask_never_that_it_cannot():
    text = _prompt("reporter")
    assert not re.search(r"nothing you can create|cannot build|can.t build|build it yourself|no write tools", text, re.I)
    assert re.search(r"draft(?:s|ed)? a plan", text) and "check" in text, "it says a plan is drafted for them to check"


def test_the_talk_step_names_no_role_either():
    assert not ROLES.search(_prompt("talker"))
