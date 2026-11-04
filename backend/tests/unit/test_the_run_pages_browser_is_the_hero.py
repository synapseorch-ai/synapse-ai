"""The run page's browser is the picture, not a strip of it (F1356; Naveen 11-04; eb).

At 0c89c76f the run page gave its pane a fixed `h-[30rem]`: the header, the status strip and the action list left the
page ≈ 170–220 px tall, letterboxed, with no way to enlarge it. The Activity log repeated every action with a
thumbnail, and after the run a strip of small cards (Filmstrip) took the pane's place. Now, read from the source here
(the OSS frontend has no unit runner; `scripts/ui-smoke.py`'s runbrowser checks hold what the browser draws):

- the run page's pane is `hero` — the picture fills the column at the page's own aspect, the actions folded into
  "What the agent did · N", closed — and can be enlarged (a dialog: Esc closes it, Take over is in it);
- every other place the pane is mounted (the chat, the sessions and recordings screens) draws as before: both are the
  run page's to ask for;
- after the run, one large viewer of the kept screenshots, back and next, its caption and "3 of 12", and Enlarge;
  Filmstrip is gone;
- the Activity log has one line per browser session, "Browser · N actions", which opens, and no thumbnail per action.
"""
from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[3] / "frontend" / "src" / "components"


def _read(path: str) -> str:
    return (SRC / path).read_text()


def test_the_run_pages_pane_is_the_hero_and_can_be_enlarged():
    run = _read("run/RunActivity.tsx")
    assert "h-[30rem]" not in run, "no fixed height: the picture sets it"
    pane = run[run.index("<BrowserPane"):].split("/>", 1)[0]
    assert re.search(r"\bhero\b", pane) and re.search(r"\benlarge\b", pane), pane


def test_the_pane_asks_for_both_and_draws_as_before_without_them():
    pane = _read("browser/BrowserPane.tsx")
    assert re.search(r"hero\?: boolean", pane) and re.search(r"enlarge\?: boolean", pane)
    assert "What the agent did" in pane and "<details" in pane, "the actions fold away on the run page"
    assert not re.search(r"<details[^>]*\bopen\b", pane), "closed by default"
    assert "Enlarge" in pane and "Take over" in pane
    for mount in ("browser/SessionsLibrary.tsx", "browser/RecordingsLibrary.tsx", "browser/RecordingEditor.tsx"):
        opened = _read(mount)[_read(mount).index("<BrowserPane"):].split("/>", 1)[0]
        assert not re.search(r"\b(hero|enlarge)\b", opened), mount


def test_after_the_run_one_large_viewer_takes_the_strips_place():
    run = _read("run/RunActivity.tsx")
    assert "<ShotViewer" in run and "Filmstrip" not in run
    assert not (SRC / "browser" / "Filmstrip.tsx").exists()
    viewer = _read("browser/ShotViewer.tsx")
    assert " of " in viewer and "Enlarge" in viewer
    assert "Previous screenshot" in viewer and "Next screenshot" in viewer


def test_the_activity_log_has_one_line_per_browser_session():
    log = _read("run/RunLogView.tsx")
    assert '"browser_group"' in log and "Browser ·" in log
    assert "function BrowserRow" not in log, "no line and thumbnail per action"
