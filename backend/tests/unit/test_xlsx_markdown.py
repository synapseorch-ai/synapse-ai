"""`parse_xlsx` renders a sheet as a Markdown table without `tabulate` (F172).

It used `DataFrame.to_markdown`, which imports `tabulate` — a package no
manifest names — so every download that succeeded ended in *`Import tabulate`
failed*, on every install and on the cloud fleet.
"""
from __future__ import annotations

import io

import pandas as pd

from tools.xlsx_parser import _markdown_table


def _roundtrip(df: pd.DataFrame) -> pd.DataFrame:
    buf = io.BytesIO()
    df.to_excel(buf, index=False)
    buf.seek(0)
    return pd.read_excel(buf)


def test_a_sheet_is_a_markdown_table():
    df = _roundtrip(pd.DataFrame({"name": ["alpha", "beta"], "qty": [1, 2]}))
    assert _markdown_table(df).splitlines() == [
        "| name | qty |",
        "| --- | --- |",
        "| alpha | 1 |",
        "| beta | 2 |",
    ]


def test_an_empty_cell_is_empty_not_nan():
    df = _roundtrip(pd.DataFrame({"a": ["x", None, "y"], "b": [1.5, None, 2.5]}))
    assert _markdown_table(df).splitlines()[3] == "|  |  |"


def test_a_pipe_or_newline_in_a_cell_cannot_break_the_table():
    df = pd.DataFrame({"note": ["a|b", "line1\nline2"]})
    lines = _markdown_table(df).splitlines()
    assert lines[2] == "| a\\|b |"
    assert lines[3] == "| line1 line2 |"
