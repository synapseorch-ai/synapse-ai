"""A run's card sits inside a phone's screen (F1760).

The card (`ApprovalCard`) is a grid item in the run page's column and in Home's list, and it kept CSS's default
`min-width: auto`: a grid item is never narrower than its content's smallest width, and with its own `1fr` column that
was its content's one-line width, the buttons' row. The column grew with it, 408 px wide on a 390 px screen, with the
fields and the buttons cut off at the right. Each of its two shapes, a question and a browser turn, is `min-w-0`, so
the column it sits in decides its width. ui-smoke's `cardonphone` measures it in the browser.
"""
from __future__ import annotations

import re
from pathlib import Path

CARD = Path(__file__).resolve().parents[3] / "frontend/src/components/run/ApprovalCard.tsx"


def test_each_shape_of_the_card_shrinks_to_its_column():
    sections = re.findall(r'<section aria-label="Waiting for a person" className="([^"]*)"',
                          CARD.read_text(encoding="utf-8"))
    assert len(sections) == 2, "the card's two shapes, a question and a browser turn"
    for classes in sections:
        assert "min-w-0" in classes.split(), f"a shape of the card grows past its column: {classes}"
