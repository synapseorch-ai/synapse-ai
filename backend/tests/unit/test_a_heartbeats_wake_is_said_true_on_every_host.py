"""How a heartbeat's wake is said is true on every host (F1749, F1757).

A heartbeat with `wake_on_new_task` is started by a new task on its board instead of at its next check. A self-hosted
scheduler looks every 30 seconds; Cloud waits at least five minutes after a schedule's last run, and its own Schedules
form says so. Every word the product shows a person says one wording that holds on both (`objects.WAKE_WORDS`), never
"within a minute": the card a person approves, the field the planner fills, the tool descriptions Synapse AI reads, the
planner's Building 5, the CLI's `--wake` help, and the self-hosted Schedules form. The form says the card's words
because a person reads both about one schedule, and two wordings for one switch read as two behaviours (F1757).
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from core.native_builder import objects as O

ROOT = Path(__file__).resolve().parents[3]
BACKEND = ROOT / "backend"
SCHEDULES_FORM = ROOT / "frontend/src/components/settings/SchedulesTab.tsx"
_SHOWN = (".py", ".json", ".md", ".ts", ".tsx")


def _shown_to_a_person() -> list[Path]:
    """The product's own files a person's words come from: the engine, the CLI, the screens and the docs; not tests."""
    listed = subprocess.run(["git", "ls-files", "backend", "synapse", "frontend/src", "docs", "README.md"],
                            cwd=ROOT, capture_output=True, text=True, check=True).stdout.split()
    return [ROOT / p for p in listed
            if p.endswith(_SHOWN) and not p.startswith("backend/tests/")
            and ".test." not in p and "__tests__" not in p and ".spec." not in p]


def test_the_files_read_are_the_products_own():
    """The near miss: a scan that lists nothing passes on anything."""
    files = {str(p.relative_to(ROOT)) for p in _shown_to_a_person()}
    assert {"backend/core/native_builder/render.py", "backend/core/builder_tools.py",
            "backend/core/native_builder/agents/planner.json", "synapse/client/commands.py",
            "frontend/src/components/settings/SchedulesTab.tsx"} <= files
    assert not any(f.startswith("backend/tests/") for f in files)


def test_nothing_a_person_reads_says_a_wake_comes_within_a_minute():
    found = [str(p.relative_to(ROOT)) for p in _shown_to_a_person()
             if "within a minute" in p.read_text("utf-8", errors="replace").lower()]
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


def test_the_schedules_form_says_the_cards_words():
    """The line under the form's "Start when a task is added" is the card's words: one switch, one wording."""
    assert O.WAKE_WORDS[0].upper() + O.WAKE_WORDS[1:] + "." in SCHEDULES_FORM.read_text("utf-8")
