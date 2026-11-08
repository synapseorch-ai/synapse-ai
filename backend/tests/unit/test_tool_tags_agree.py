"""The tool editor's method table is the engine's (TAG1, D296, F738).

The editor shows what a save will store from `frontend/src/lib/toolHints.ts`'s `METHOD_TAGS`; the backend stores
`core/tool_tags.py`'s. A change on one side only shows the person one thing and saves another. The file is synced
into cloud's web, so this holds both products' editors to the engine.
"""
from __future__ import annotations

import re
from pathlib import Path

from core.tool_tags import METHOD_TAGS, TAGS

FRONTEND = Path(__file__).resolve().parents[3] / "frontend" / "src"


def _object(text: str, consts: dict[str, dict]) -> dict:
    """`{ ...WRITE, idempotent: true }`, `READ`, or `{ read_only: true, … }` as a dict."""
    text = text.strip()
    if text in consts:
        return dict(consts[text])
    body = text.strip("{} ")
    out: dict = {}
    for part in (p.strip() for p in body.split(",") if p.strip()):
        if part.startswith("..."):
            out.update(consts[part[3:].strip()])
        else:
            key, value = (x.strip() for x in part.split(":"))
            out[key] = {"true": True, "false": False}[value]
    return out


def test_the_editors_method_table_is_the_engines():
    source = (FRONTEND / "lib" / "toolHints.ts").read_text("utf-8")
    consts: dict[str, dict] = {}
    for name, body in re.findall(r"const (READ|WRITE): ToolTags = (\{[^}]+\});", source):
        consts[name] = _object(body, consts)
    table = re.search(r"export const METHOD_TAGS: Record<string, ToolTags> = \{(.+?)\n\};", source, re.S)
    assert table, "METHOD_TAGS is gone from frontend/src/lib/toolHints.ts"
    entries = re.findall(r"([A-Z]+): (\{[^}]+\}|READ|WRITE)", table.group(1))
    editor = {method: _object(body, consts) for method, body in entries}
    assert editor == METHOD_TAGS
    assert all(set(tags) == set(TAGS) for tags in editor.values())
