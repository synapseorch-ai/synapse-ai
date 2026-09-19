"""Which orchestration a builder message is about.

The builder's router may only choose `update` when `current_orchestration_id`
is set, and for as long as that rule existed nothing set it reliably: the
commercial editor never sent the open orchestration, and neither product turned
a name in the message into an id. "Update the second step … on the extended
research orchestration" was routed to `create` five times in one testing round,
the router quoting its own rule back each time.

This resolves the target from two signals — the orchestration open on screen
and a name (or id) in the message — into state keys the seed's steps read. It
is pure, and it trusts its caller for one thing only: `visible` holds exactly
the orchestrations the builder may touch. A system row, a retired one, another
tenant's, a draft that was never saved: not in the list, so never a target.

When the signals disagree, or a name fits two orchestrations, nothing resolves
and the router sends the message to `answer`, which asks. Guessing wrong here
edits someone's workflow they did not mean to touch.

The next message can answer that question (F16). Given the list the previous
turn asked about, a reply that is nothing but a choice — "the second one",
"2" — or that names exactly one of those candidates resolves to it, as
`picked`, even with another orchestration open: the person has now said which.
Anything else is resolved as if nothing had been asked.
"""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

VIEWING = "viewing"
NAMED = "named"
AMBIGUOUS = "ambiguous"
#: Chosen from the list the previous turn asked about. The request itself is
#: that turn's — the router reads it from `chat_history`.
PICKED = "picked"

#: The state keys this module fills. Declared in the seed's `state_schema`; the
#: commercial edition sets them from its own RLS-scoped rows.
TARGET_KEYS = (
    "current_orchestration_id",
    "current_orchestration_name",
    "current_orchestration_source",
    "current_orchestration_matches",
    "current_page",
)


@dataclass(frozen=True)
class Target:
    id: str = ""
    name: str = ""
    source: str = ""
    #: One line per candidate when `source` is `ambiguous`.
    matches: tuple[str, ...] = ()

    def state(self) -> dict[str, str]:
        """The orchestration keys, as the run's shared state holds them."""
        return {
            "current_orchestration_id": self.id,
            "current_orchestration_name": self.name,
            "current_orchestration_source": self.source,
            "current_orchestration_matches": "\n".join(self.matches),
        }


def _words(text: str) -> str:
    """Lower-case letters and digits, one space between runs."""
    folded = unicodedata.normalize("NFKC", text or "").casefold()
    return " ".join(re.findall(r"[^\W_]+", folded))


def _spans(haystack: str, needle: str) -> list[tuple[int, int]]:
    """Where `needle` occurs in `haystack` as whole words (both normalised)."""
    if not needle:
        return []
    pattern = rf"(?<!\S){re.escape(needle)}(?!\S)"
    return [m.span() for m in re.finditer(pattern, haystack)]


def _named(message: str, visible: list[Mapping]) -> list[Mapping]:
    """The orchestrations the message names, by name or by id, in list order."""
    words = _words(message)
    hits: dict[str, list[tuple[int, int]]] = {}
    for row in visible:
        spans = _spans(words, _words(str(row.get("name") or "")))
        if spans:
            hits[row["id"]] = spans

    def inside(span: tuple[int, int], other_id: str) -> bool:
        return any(
            s[0] <= span[0] and span[1] <= s[1] and (s[1] - s[0]) > (span[1] - span[0])
            for s in hits[other_id]
        )

    # "research" inside "extended research" is not a second name: drop a match
    # only when every place it occurs sits inside a longer match.
    named = {
        orch_id for orch_id, spans in hits.items()
        if not all(any(inside(span, other) for other in hits if other != orch_id) for span in spans)
    }
    for row in visible:
        if re.search(rf"(?<![\w-]){re.escape(row['id'])}(?![\w-])", message or ""):
            named.add(row["id"])
    return [row for row in visible if row["id"] in named]


def _line(row: Mapping, why: str) -> str:
    return f"{row.get('name') or row['id']} ({row['id']}) — {why}"


def _ids_in(lines: Iterable[str]) -> list[str]:
    """The ids in a previous turn's matches, in the order it listed them."""
    out = []
    for line in lines:
        found = re.search(r"\(([^()\s]+)\)\s*$", line.rsplit(" — ", 1)[0])
        if found:
            out.append(found.group(1))
    return out


_ORDINALS = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
    "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10,
}
#: Words a bare choice comes wrapped in. Anything else in the reply makes it a
#: request rather than a pick: "add 2 steps" is not "2".
_FILLER = frozenset(
    "the one please i m mean its it is number no option pick choose that ok okay yes go with use".split()
)


def _ordinal(message: str, count: int) -> int | None:
    """The 0-based position a reply like "the second one" or "#2" picks, or None."""
    words = [w for w in _words(message).split() if w not in _FILLER]
    if len(words) != 1:
        return None
    word = words[0]
    if word == "last":
        n = count
    elif word.isdigit():
        n = int(word)
    elif re.fullmatch(r"\d+(st|nd|rd|th)", word):
        n = int(word[:-2])
    else:
        n = _ORDINALS.get(word, 0)
    return n - 1 if 1 <= n <= count else None


def resolve_target(
    message: str,
    viewing_id: str | None,
    visible: Iterable[Mapping],
    previous_matches: Iterable[str] | str = (),
) -> Target:
    """The orchestration `message` is about, from what is open and what is named.

    `visible` rows need `id` and `name`, and must already exclude everything the
    builder may not touch. `previous_matches` is the list the previous turn of
    this conversation asked about (its `current_orchestration_matches`, as lines
    or as the stored text), or nothing when that turn was not a question.
    """
    rows = [row for row in visible if row.get("id")]
    viewing = next((row for row in rows if row["id"] == viewing_id), None) if viewing_id else None
    named = _named(message, rows)

    asked = _ids_in(
        previous_matches.splitlines() if isinstance(previous_matches, str) else previous_matches
    )
    if asked:
        by_id = {row["id"]: row for row in rows}
        picked = None
        if named:
            if len(named) == 1 and named[0]["id"] in asked:
                picked = named[0]
        else:
            index = _ordinal(message, len(asked))
            if index is not None:
                picked = by_id.get(asked[index])
        if picked is not None:
            return Target(picked["id"], str(picked.get("name") or ""), PICKED)

    if not named:
        if viewing is None:
            return Target()
        return Target(viewing["id"], str(viewing.get("name") or ""), VIEWING)

    if len(named) == 1 and (viewing is None or named[0]["id"] == viewing["id"]):
        return Target(named[0]["id"], str(named[0].get("name") or ""), NAMED)

    lines = []
    if viewing is not None and all(row["id"] != viewing["id"] for row in named):
        lines.append(_line(viewing, "open on screen"))
    lines.extend(_line(row, "named in the message") for row in named)
    return Target(source=AMBIGUOUS, matches=tuple(lines))


def describe_page(surface: str, name: str, ident: str = "") -> str:
    """One line saying what is open on screen, for the steps that read `current_page`."""
    label = f'the {surface} "{name}"' if name else f"a {surface}"
    return f"The user has {label}{f' ({ident})' if ident else ''} open on screen."
