"""How a heartbeat's wake is said is true on every host (F1749).

A heartbeat with `wake_on_new_task` is started by a new task on its board instead of at its next check. A self-hosted
scheduler looks every 30 seconds; Cloud waits at least five minutes after a schedule's last run, and its own Schedules
form says so. The builder, its card, the planner's prompt and the CLI run on both, so each says one wording that holds
on both (`objects.WAKE_WORDS`), never "within a minute": the card a person approves, the field the planner fills, the
tool descriptions Synapse AI reads, the planner's Building 5, and the CLI's `--wake` help. The self-hosted Schedules
screen, drawn only where the scheduler looks every 30 seconds, keeps its own words.
"""
from __future__ import annotations

import json
from pathlib import Path

from core.native_builder import objects as O

ROOT = Path(__file__).resolve().parents[3]
BACKEND = ROOT / "backend"
SAID_ON_BOTH = [BACKEND / "core/native_builder/render.py", BACKEND / "core/native_builder/objects.py",
                BACKEND / "core/builder_tools.py", BACKEND / "core/native_builder/agents/planner.json",
                ROOT / "synapse/client/commands.py"]
OSS_SCREEN = ROOT / "frontend/src/components/settings/SchedulesTab.tsx"


def test_nothing_both_hosts_run_says_a_wake_comes_within_a_minute():
    found = [str(p.relative_to(ROOT)) for p in SAID_ON_BOTH if "within a minute" in p.read_text("utf-8")]
    assert found == []


def test_each_says_the_one_wording():
    assert O.WAKE_WORDS == "a new task starts it early, without waiting for its next check"
    planner = json.loads((BACKEND / "core/native_builder/agents/planner.json").read_text("utf-8"))
    assert O.WAKE_WORDS in json.dumps(planner, ensure_ascii=False), "the planner's prompt says the card's words"
    assert O.WAKE_WORDS in O.Schedule.model_fields["run_when"].description
    tools = (BACKEND / "core/builder_tools.py").read_text("utf-8")
    assert tools.count("A heartbeat only: {_WAKE_WORDS}") == 2, "set_schedule's and create_schedule's descriptions"
    assert O.WAKE_WORDS in (ROOT / "synapse/client/commands.py").read_text("utf-8")


def test_the_card_says_it():
    from core.native_builder.render import render

    plan = O.Plan.model_validate({
        "kind": "create", "title": "Harbor & Pine requests", "summary": "Handles each request.",
        "steps": [{"id": "work", "type": "llm", "name": "Work", "does": "Works the request", "prompt": "Work it."}],
        "schedule": {"cron": "*/15 * * * *", "says": "every 15 minutes", "run_when": "has_work"}})
    assert f"so a quiet day costs nothing; {O.WAKE_WORDS}." in render(plan)


def test_the_self_hosted_schedules_screen_keeps_its_own_words():
    """Drawn only where the scheduler looks every 30 seconds: true there, and Cloud draws its own form."""
    assert "A new task starts it within a minute" in OSS_SCREEN.read_text("utf-8")
