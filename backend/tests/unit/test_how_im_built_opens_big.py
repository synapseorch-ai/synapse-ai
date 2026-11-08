""""See how I'm built" opens big (F1390; Naveen 11-05; eb).

At 3789adc6 the window was `Modal size="lg"` (max-w-2xl, 672 px) and the builder's own workflow sat in a fixed `h-80`
(320 px) box above its explanation: the picture the window is for was the smallest thing in it. Now, read from the
source here (the OSS frontend has no unit runner; `scripts/ui-smoke.py`'s `howimbuilt` holds what the browser draws,
at 1440 and on a phone):

- it opens in Enlarge's full window (`size="full"`), the one the product already has for a thing looked at large;
- the canvas is sized from the screen, most of the window's height, never below the 320 px it had;
- the explanation (where it starts, where it stops for you, the sub-agents, the step types) is below the canvas, in
  the window's body that scrolls, and the footer keeps Close, Duplicate and the step count, which may wrap on a phone.
"""
from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[3] / "frontend" / "src" / "components"


def _how() -> str:
    return (SRC / "app" / "HowImBuilt.tsx").read_text()


def _opened(source: str) -> str:
    return source[source.index("<Modal"):].split(">", 1)[0]


def _canvas(source: str) -> str:
    return source[:source.index("<WorkflowCanvas")].rsplit("<div", 1)[1]


def test_it_opens_in_the_full_window():
    assert re.search(r'size="full"', _opened(_how())), _opened(_how())


def test_the_full_window_is_enlarges_own():
    dialog = (SRC / "ui" / "dialog.tsx").read_text()
    assert re.search(r'full: "[^"]*max-w-\[min\(96vw,1600px\)\]', dialog), "the window Enlarge opens"


def test_the_canvas_is_sized_from_the_screen_never_below_its_old_height():
    box = _canvas(_how())
    assert not re.search(r"(?<![\w-])h-80\b", box), "no fixed 320 px: " + box
    assert re.search(r"\bh-\[\d+(?:\.\d+)?d?vh\]", box), box
    assert re.search(r"\bmin-h-80\b", box), box


def test_the_explanation_is_below_the_canvas():
    how = _how()
    canvas = how.index("<WorkflowCanvas")
    for heading in ("Where it starts", "Where it stops for you", "The sub-agents", "Step types it uses"):
        assert how.index(heading) > canvas, heading


def test_every_option_it_had_is_kept():
    how = _how()
    for kept in ("Duplicate to my workspace", ">Close<", "step_count", "sub-agents", "{error &&", "<Spinner",
                 "readOnly", "/api/builder/duplicate", "/api/builder/legend"):
        assert kept in how, kept
