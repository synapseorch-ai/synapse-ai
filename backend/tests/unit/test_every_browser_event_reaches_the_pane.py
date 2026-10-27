"""Every browser event the backend makes public is read by both screens that show it (BRW2, D330, F1073/F1074).

The browser component announces a session (`browser_session`) and each agent action (`browser_action`) on the chat
turn stream and the run journal (`core/events_public.py`). A screen that has no case for one drops it without a word:
the chat's `handleSSEEvent` and the run log's `applyRunEvent` both end in a silent default. So BRW1's live check
saw the agent read the page while the pane never heard it open. A `browser_*` type made public later fails here
until both screens read it.
"""
from __future__ import annotations

import re
from pathlib import Path

from core.events_public import PUBLIC_EVENT_TYPES

FRONTEND = Path(__file__).resolve().parents[3] / "frontend" / "src"

#: Where each screen reads a stream's events, and the function that does.
READERS = {
    "chat": (FRONTEND / "app" / "(app)" / "page.tsx", "handleSSEEvent"),
    "run log": (FRONTEND / "components" / "run" / "RunLogView.tsx", "applyRunEvent"),
}


def _browser_events() -> list[str]:
    return sorted(t for t in PUBLIC_EVENT_TYPES if t.startswith("browser_"))


def _cases(path: Path, fn: str) -> set[str]:
    source = path.read_text("utf-8")
    start = source.index(fn)
    return set(re.findall(r"""case ['"]([a-z_]+)['"]""", source[start:]))


def test_the_browser_announces_itself():
    assert {"browser_session", "browser_action"} <= set(_browser_events())


def test_every_screen_reads_every_browser_event():
    missing = [f"{where} ({fn}) drops {event}"
               for where, (path, fn) in READERS.items()
               for event in _browser_events()
               if event not in _cases(path, fn)]
    assert not missing, "; ".join(missing)
