"""A sheet as the index sees it: a "sheet card", and row groups for a small sheet (16 §3.5).

One builder for every tabular source — CSV/TSV (`text.py`), XLSX and ODS — so
a sheet reads the same whichever file it came in. The card names the sheet,
its headers and its row count; dense retrievers find the right table only
from a description like this (16 §3.5). A sheet up to `ROW_GROUP_MAX_ROWS`
data rows is also indexed `ROW_GROUP_SIZE` rows at a time, each group under
the header; a bigger one is its card, with a warning saying so.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from core.knowledge.parsed import ParseWarning, SheetData, TableData

ROW_GROUP_MAX_ROWS = 500
ROW_GROUP_SIZE = 25
SAMPLE_ROWS = 20
#: Columns past this are left out of a sheet, with a warning. The spreadsheet
#: readers drop trailing empty columns first (a stray format on column XFD).
MAX_COLUMNS = 1024

_NUMBER = re.compile(r"^[-+]?(\d{1,3}(,\d{3})+|\d+)(\.\d+)?([eE][-+]?\d+)?$")
_DATEISH = re.compile(r"^\d{4}-\d{2}(-\d{2})?([ T]\d{2}:\d{2}(:\d{2})?)?$")


def dtype(values: list[str]) -> str:
    seen = [v.strip() for v in values if v.strip()]
    if not seen:
        return "string"
    if all(_NUMBER.match(v) for v in seen):
        return "number"
    if all(_DATEISH.match(v) for v in seen):
        return "date"
    if all(v.lower() in ("true", "false", "yes", "no") for v in seen):
        return "bool"
    return "string"


def column(n: int) -> str:
    """A 1-based column number as a spreadsheet letter: 1 → A, 27 → AA."""
    name = ""
    while n:
        n, rem = divmod(n - 1, 26)
        name = chr(65 + rem) + name
    return name or "A"


@dataclass
class SheetParts:
    card: str
    data: SheetData
    groups: list[tuple[str, TableData]] = field(default_factory=list)
    warnings: list[ParseWarning] = field(default_factory=list)


def sheet_parts(name: str, rows: list[list[str]]) -> SheetParts | None:
    """The card and the row groups for one sheet; None when it has no non-empty row.

    The first non-empty row is the header. Ragged rows are padded to the widest.
    """
    rows = [r for r in rows if any(c.strip() for c in r)]
    if not rows:
        return None
    warnings: list[ParseWarning] = []
    width = max(len(r) for r in rows)
    if width > MAX_COLUMNS:
        warnings.append(ParseWarning(code="sheet_truncated", count=width - MAX_COLUMNS, message=(
            f"Sheet {name}: {width} columns; only the first {MAX_COLUMNS} are indexed.")))
        width = MAX_COLUMNS
        rows = [r[:width] for r in rows]
    header = rows[0] + [""] * (width - len(rows[0]))
    data = [r + [""] * (width - len(r)) for r in rows[1:]]
    dtypes = [dtype([r[c] for r in data[:1000]]) for c in range(width)]
    card = f"Sheet {name}: {', '.join(h or column(c + 1) for c, h in enumerate(header))} ({len(data)} rows)"
    parts = SheetParts(card=card, warnings=warnings, data=SheetData(
        name=name, range=f"A1:{column(width)}{len(rows)}", n_rows=len(data), n_cols=width, headers=header,
        sample_rows=data[:SAMPLE_ROWS], dtypes=dtypes, truncated=len(data) > ROW_GROUP_MAX_ROWS))
    if len(data) <= ROW_GROUP_MAX_ROWS:
        for start in range(0, len(data), ROW_GROUP_SIZE):
            group = data[start:start + ROW_GROUP_SIZE]
            body = "\n".join(" | ".join(r) for r in [header, *group])
            parts.groups.append((body, TableData(rows=[header, *group], header_rows=1, n_rows=len(group) + 1,
                                                 n_cols=width)))
    elif data:
        warnings.append(ParseWarning(code="sheet_truncated", count=len(data), message=(
            f"{len(data)} rows: the sheet is indexed as its card (headers, types, {SAMPLE_ROWS} sample rows), "
            f"not row by row, above {ROW_GROUP_MAX_ROWS} rows.")))
    return parts
