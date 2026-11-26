"""A run's card sits inside a phone's screen (F1760).

The card (`ApprovalCard`) is a grid item in the run page's column and in Home's list, and it kept CSS's default
`min-width: auto`: a grid item is never narrower than its content's smallest width, and with its own `1fr` column that
was its content's one-line width, the buttons' row. And the run page's column was `auto`, so its widest item's
smallest width set it: the Activity log's unbroken run id, 415 px on a 390 px screen, with the card's fields and
buttons cut off at the right. The run page's column is `minmax(0, 1fr)`, so the page's width decides it, and each of
the card's two shapes, a question and a browser turn, is `min-w-0`, so any column it sits in decides its width.
ui-smoke's `cardonphone` measures it in the browser.
"""
from __future__ import annotations

import re
from pathlib import Path

RUN = Path(__file__).resolve().parents[3] / "frontend/src/components/run"
CARD = RUN / "ApprovalCard.tsx"


def test_each_shape_of_the_card_shrinks_to_its_column():
    sections = re.findall(r'<section aria-label="Waiting for a person" className="([^"]*)"',
                          CARD.read_text(encoding="utf-8"))
    assert len(sections) == 2, "the card's two shapes, a question and a browser turn"
    for classes in sections:
        assert "min-w-0" in classes.split(), f"a shape of the card grows past its column: {classes}"


def test_the_run_pages_column_is_as_wide_as_the_page():
    source = (RUN / "RunDetail.tsx").read_text(encoding="utf-8")
    assert '<div className="grid grid-cols-[minmax(0,1fr)] gap-7">' in source, \
        "the run page's column grows to its widest item: a log's unbroken id pushes the card past a phone"
