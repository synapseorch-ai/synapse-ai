"""What a change does, field by field: one row each, as it stands and as it will be (F1262; TRI1's one shape, 10-30).

An act's card (`builder_actions`), a write's review line (`change_tools`) and a setting's card (F1264) all show a
change the same way, so a person reads one shape wherever Synapse AI asks for a yes: `{label, before, after}`, and a
short `note` when a row needs one ("indexed again, about $0.04"). `before` None is something new; `after` None is
something that goes. Values are display text: code puts them here, never an id, a key or a secret.

Nothing here knows about a card. The card renders `rows` as they are (`ChangeRows.tsx`); a review takes `as_text`.
"""
from __future__ import annotations

from typing import Any, TypedDict


class Row(TypedDict, total=False):
    label: str
    before: str | None
    after: str | None
    note: str


#: A value longer than this, or on more than one line, is shown as two lines with its changed words marked.
SHORT = 80
#: How much of what is kept is shown on each side of the changed words.
_AROUND = 60
#: The most of the changed words a line shows.
_MOST = 600


def say(value: Any) -> str | None:
    """A value as a row shows it: on/off, a list joined, nothing as None."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "on" if value else "off"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value) if value else "none"
    text = " ".join(str(value).split()) if "\n" not in str(value) else str(value).strip()
    return text if text else None


def changed(before: dict[str, Any] | None, after: dict[str, Any], labels: dict[str, str]) -> list[Row]:
    """One row per labelled field whose value differs, in the order of `labels`. `before` None: all of it is new."""
    out: list[Row] = []
    for key, label in labels.items():
        if key not in after:
            continue
        was = say((before or {}).get(key)) if before is not None else None
        now = say(after[key])
        if before is not None and was == now:
            continue
        if before is None and now is None:
            continue
        out.append({"label": label, "before": was, "after": now})
    return out


def marked(before: str, after: str) -> tuple[str, str]:
    """Both texts cut to the words that differ, marked with brackets, with a little of what is kept around them
    (F1258's section line): "…within [30 days] for a full refund…"."""
    start = 0
    while start < min(len(before), len(after)) and before[start] == after[start]:
        start += 1
    end = 0
    while end < min(len(before), len(after)) - start and before[-1 - end] == after[-1 - end]:
        end += 1
    # Whole words: the marks go around "30 days", never inside "[3]0".
    while start > 0 and not before[start - 1].isspace():
        start -= 1
    while end > 0 and not before[len(before) - end].isspace():
        end -= 1

    def part(text: str) -> str:
        middle = text[start:len(text) - end]
        lead = ("…" if start > _AROUND else "") + text[max(0, start - _AROUND):start]
        tail = text[len(text) - end:len(text) - end + _AROUND] + ("…" if end > _AROUND else "")
        if len(middle) > _MOST:
            middle = middle[:_MOST - 3] + "…"
        return " ".join(f"{lead}[{middle}]{tail}".split())

    return part(before), part(after)


def _long(value: str | None) -> bool:
    return value is not None and (len(value) > SHORT or "\n" in value)


def as_text(rows: list[Row], indent: str = "  ") -> str:
    """The rows as a review's lines: "label: before → after"; a long value as two lines, its change marked (one that
    is marked already, with brackets, as it is)."""
    lines: list[str] = []
    for row in rows:
        label, was, now = row.get("label", ""), row.get("before"), row.get("after")
        if was is None:
            lines.append(f"{indent}{label}: {now}" if not _long(now) else f"{indent}{label}, new: {now}")
        elif now is None:
            lines.append(f"{indent}{label}: {was} → (none)")
        elif _long(was) or _long(now):
            if "[" not in was and "[" not in now:
                was, now = marked(was, now)
            lines += [f"{indent}{label}, before: {was}", f"{indent}{label}, after: {now}"]
        else:
            lines.append(f"{indent}{label}: {was} → {now}")
        if row.get("note"):
            lines.append(f"{indent}  {row['note']}")
    return "\n".join(lines)
