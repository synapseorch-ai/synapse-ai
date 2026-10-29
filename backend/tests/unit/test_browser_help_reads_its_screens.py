"""The "?" has a topic for Recordings and for the Browser step, and their screens name it (GUIDE1, F1190).

Agents › Browser had one topic, about sessions; its Recordings tab, a recording's page and a workflow's Browser step
showed it too, or the editor's. Each now says what is on it: the Recordings list and a recording's page name
`browser.recordings`, and the Browser step's card `orchestrations.browser_step`, through `useHelpTopic` as Boards and
Knowledge do. All three are in the synced kit, so cloud's topics of the same ids show there. The search_help copy
(`help_topics.json`) carries both, as the builder answers how-to questions from it.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "frontend" / "src"
HELP = SRC / "lib" / "help.ts"
TOPICS_JSON = ROOT / "backend" / "core" / "native_builder" / "help_topics.json"

TOPICS = {"browser.recordings": "Recordings", "orchestrations.browser_step": "A Browser step"}
SCREENS = {
    "components/browser/RecordingsLibrary.tsx": "browser.recordings",
    "components/browser/RecordingEditor.tsx": "browser.recordings",
    "components/orchestration/panel/steps.tsx": "orchestrations.browser_step",
}


def _block(key: str) -> str:
    text = HELP.read_text("utf-8")
    head = re.search(rf"(?m)^    '?{re.escape(key)}'?: \{{", text)
    assert head, f"no {key} topic in help.ts"
    end = re.search(r"(?m)^    \},?$", text[head.end():])
    return text[head.end(): head.end() + end.start()]


def test_each_topic_is_in_the_help():
    for key, title in TOPICS.items():
        assert f"title: '{title}'" in _block(key), f"{key} is not titled {title!r}"


def test_each_screen_names_its_topic():
    for path, key in SCREENS.items():
        assert f"useHelpTopic('{key}')" in (SRC / path).read_text("utf-8"), f"{path} does not name {key}"


def test_the_browser_step_names_it_inside_its_own_section():
    # The step panel holds every step type's section: only the Browser step's may name the topic.
    text = (SRC / "components/orchestration/panel/steps.tsx").read_text("utf-8")
    section = text[text.index("function BrowserStepSection("):]
    section = section[: section.index("\nexport const STEP_SECTIONS")]
    assert "useHelpTopic('orchestrations.browser_step')" in section


def test_the_search_help_copy_says_the_same():
    data = json.loads(TOPICS_JSON.read_text("utf-8"))
    assert {key: data.get(key, {}).get("title") for key in TOPICS} == TOPICS
