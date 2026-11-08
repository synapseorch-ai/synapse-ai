"""A change's review shows its code as code: a fenced block in a step's detail is a box of its own (F1328; Naveen's
pick (A), 11-04).

F1325's review carries a Python tool's diff and its code as it will be, as fenced blocks in the change's line. The
panel's card drew every step's detail as one line of plain text, so the blocks came out as ```diff … ``` with their
lines run together (F1325's shots, 1440 and 390). Now `plan.detailParts` splits a detail into its text and its fenced
blocks, and the card draws each block as a <pre>: in the code font, its lines kept, scrolling sideways on a phone, a
diff's added and removed lines tinted with the design's own success and danger tokens. The code font is `font-code`:
here `font-mono` is aliased to the UI font (globals.css), which F1328's first shots showed as a box of Inter. The rest of the detail is text, as it
was. The OSS frontend has no unit runner, so this holds the parts by their source; `scripts/ui-smoke.py`'s
`check_codeblock` holds what the browser draws.
"""
from __future__ import annotations

import re
from pathlib import Path

ASSISTANT = Path(__file__).resolve().parents[3] / "frontend" / "src" / "components" / "assistant"


def _source(name: str) -> str:
    return (ASSISTANT / name).read_text()


def test_a_detail_is_split_into_its_text_and_its_fenced_blocks():
    plan = _source("plan.ts")
    assert re.search(r"export function detailParts\(detail: string\): DetailPart\[\]", plan), \
        "one place reads a detail's fenced blocks"
    assert "```" in plan.split("export function detailParts", 1)[1][:1200], "it reads ``` fences"


def test_a_fenced_block_is_a_pre_that_keeps_its_lines_and_scrolls_sideways():
    cards = _source("cards.tsx")
    assert "detailParts(" in cards, "the card draws a step's detail by its parts"
    pre = re.search(r"<pre[^>]*className=\"([^\"]+)\"", cards)
    assert pre, "a block is a <pre>"
    for word in ("whitespace-pre", "overflow-x-auto", "font-code"):
        assert word in pre.group(1).split(), (word, pre.group(1))
    assert "font-mono" not in pre.group(1).split(), "font-mono is the UI font here (globals.css): code is font-code"


def test_a_diffs_added_and_removed_lines_are_tinted_with_the_designs_tokens():
    cards = _source("cards.tsx")
    for token in ("bg-success-subtle", "text-success", "bg-danger-subtle", "text-danger"):
        assert token in cards.split("function StepDetail", 1)[-1], token
