"""A list's detail pane fits a phone, however long its title or its path (F1741).

`DetailHead` heads a detail pane: its title and line `truncate` (one line, cut with an ellipsis). Its row was a grid
item with the default `min-width: auto`, so a truncated line counted at its whole length: the Skills pane's file path
made the pane's one grid track 994px wide on a 390 screen, and every field under it was drawn that wide, off screen.
Now the row is `min-w-0`, so a long line is cut where the pane ends; its actions are never wider than the row, as the
page header's are (F1740); and the list-detail grid's one column below `lg` is `minmax(0,1fr)`, never wider than the
screen whatever a pane holds.
"""
from __future__ import annotations

import re
from pathlib import Path

HEAD = Path(__file__).resolve().parents[3] / "frontend/src/components/run/DetailHead.tsx"


def _detail_head() -> str:
    source = HEAD.read_text("utf-8")
    return source[source.index("export function DetailHead("):source.index("export const LIST_DETAIL")]


def test_the_head_row_lets_a_long_line_be_cut():
    row = re.search(r'return \(\s*<div className="([^"]+)">', _detail_head())
    assert row, "DetailHead's row"
    assert "min-w-0" in row.group(1).split(), row.group(1)


def test_its_actions_are_never_wider_than_the_row():
    actions = re.search(r'\{actions && <div className="([^"]+)">\{actions\}</div>\}', _detail_head())
    assert actions, "DetailHead's actions"
    classes = actions.group(1).split()
    assert "shrink-0" not in classes and {"min-w-0", "max-w-full", "flex-wrap"} <= set(classes), classes


def test_the_list_detail_grid_has_one_column_no_wider_than_the_screen_below_lg():
    found = re.search(r"export const LIST_DETAIL = '([^']+)'", HEAD.read_text("utf-8"))
    assert found and "grid-cols-[minmax(0,1fr)]" in found.group(1).split(), found and found.group(1)
