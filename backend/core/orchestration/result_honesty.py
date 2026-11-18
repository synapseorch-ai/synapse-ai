"""A step says when what it made is empty, built from a cut answer, or a CSV with no rows (F1690).

A run read a long page with the web reader, which keeps a page's first 50,000 characters and marks the rest as cut
(`[...truncated — N chars omitted]`); the rows it wanted were in the cut part, its parse found none, and it wrote a
CSV holding only its header. It said "completed" and nothing more. These are the lines the engine says for such a
step, as a `step_warning` and on the step's own record (`result_notes`), which make the run read "completed with
warnings":

* **A cut answer**: a Tool step's answer with a text that ends in the reader's own mark, said with how much was read.
  Only at the end of a text: a page that quotes the mark in its words was not cut.
* **An empty result**: a step that wrote nothing ("", [], {}), or a typed answer whose fields are all empty, unless
  every one of those fields is one the plan says may be empty (`may_be_empty`).
* **A CSV with no rows**: a Tool step whose arguments write `.csv` or `.tsv` text holding a header and nothing more. An
  append adds a row, never a header, so it is never said.
"""
from __future__ import annotations

import json
import re
from typing import Any

#: The web reader's own mark for a page it cut (`tools/web_scraper._truncate`), at the end of the text it kept.
_CUT = re.compile(r"\n\n\[\.\.\.truncated — (\d+) chars omitted\]\s*\Z")
#: How deep into an answer a cut text is looked for: a reader's answer holds it at its top level.
_DEPTH = 3
_TABLES = (".csv", ".tsv")


def _parsed(value: Any) -> Any:
    """A tool's answer as JSON text is read as what it says; any other text is itself."""
    if isinstance(value, str) and value.lstrip()[:1] in ("{", "["):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _cut_texts(value: Any, depth: int = 0):
    if isinstance(value, str):
        if m := _CUT.search(value):
            yield m.start(), int(m.group(1))
    elif depth < _DEPTH and isinstance(value, dict):
        for v in value.values():
            yield from _cut_texts(v, depth + 1)
    elif depth < _DEPTH and isinstance(value, list):
        for v in value:
            yield from _cut_texts(v, depth + 1)


def cut_said(name: str, value: Any) -> str | None:
    """The line for a step whose answer the reader cut short, with how much of it was read; None for any other."""
    found = next(_cut_texts(_parsed(value)), None)
    if found is None:
        return None
    kept, omitted = found
    return (f"'{name}' got a cut answer: the first {kept:,} of about {kept + omitted:,} characters. What is built "
            "from it may be partial.")


def _empty(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip()) or (
        isinstance(value, (list, dict)) and not value)


def empty_said(step: Any, value: Any) -> str | None:
    """The line for a step that made nothing; None when it made something, or when every field it left empty is one
    its plan says may be empty."""
    value = _parsed(value)
    if isinstance(value, dict) and value:
        if not all(_empty(v) for v in value.values()):
            return None
        may = {f.get("name") for f in getattr(step, "output_fields", None) or []
               if isinstance(f, dict) and f.get("may_be_empty")}
        if set(value) <= may:
            return None
    elif not _empty(value):
        return None
    return f"'{step.name}' made nothing: its result is empty."


def csv_said(name: str, args: Any) -> str | None:
    """The line for a Tool step that writes a CSV holding only its header; None for any other write."""
    if not isinstance(args, dict) or args.get("append"):
        return None
    path, content = args.get("path"), args.get("content")
    if not isinstance(path, str) or not path.lower().endswith(_TABLES) or not isinstance(content, str):
        return None
    if len([line for line in content.splitlines() if line.strip()]) > 1:
        return None
    file = path.rsplit("/", 1)[-1]
    return f"'{name}' wrote {file} with a header and no rows." if content.strip() else f"'{name}' wrote {file} empty."


#: The steps whose result is something they made, so an empty one is said; a branch's, a print's or a Loop's (F1622's)
#: is not.
MAKES = frozenset({"agent", "llm", "tool", "transform", "extract_json"})
