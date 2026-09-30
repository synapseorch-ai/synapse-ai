"""`[K#]` labels across a turn, resolved into citations with a quote check (B1b, D161).

**The labels of one turn.** Every `search_knowledge` in a turn numbers its hits
from where the turn's previous searches stopped, and a delegated agent's
searches continue the same count (`agent_contract`). `KnowledgeTurn` is that
count and what each label names. The engine makes one per root turn and hands
the same object to its delegates — explicitly, never through a ContextVar,
which an async generator would leak into its caller. It rides the turn's
snapshot (`to_state`) so a paused or crashed turn resumes with its labels.

**Citations.** When the root turn answers, `citations_event` resolves the
labels the answer used into `api_models.Citation`s — where the words are, and
how fresh they are — and lists the ones no search in this turn produced as
`unresolved`. The model never writes a path or a page itself (D153 #13).

**The quote check.** A quote the answer attributes to a label, as
`"exact words" [K3]`, is looked for in the text the model was shown under that
label: exactly, else after normalising case, quote marks, dashes, invisible
characters and whitespace; an ellipsis splits a quote into parts that must
appear in order. After a resume the shown text is rebuilt from the chunk rows,
so a passage that was cut, or listed as not shown, is checked against its
whole text. The check says whether the words are there, not whether the claim
around them is true.

**Other surfaces.** `strip_labels` takes the labels out of an answer for a
surface that cannot show citations (the widget, a messaging channel);
`neutralize` keeps a label written inside a document, or inside a delegated
workflow's answer, from reading as one of this turn's; `sanitize` is how
untrusted document text is made safe to place between the engine's markers.

Pure Python, standard library only at import: every agent's turn builds a
`KnowledgeTurn`, including the ones with no knowledge bases.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from core.knowledge.agent_contract import format_label, labels_in
from core.knowledge.errors import KnowledgeError
from core.log import get_logger

_log = get_logger(__name__)

MAX_LABEL = 999
STATE_VERSION = 1
#: Reads remembered per label, for rebuilding the quote check's text after a resume.
MAX_READS = 3
QUOTE_MAX = 300


# ── untrusted text ─────────────────────────────────────────────────────────────

_LABEL = re.compile(r"\[K(\d{1,3})\]")
_TURN_LINE = re.compile(r"(?m)^(\[Turn |System:)")


def neutralize(text: str) -> str:
    """`[K12]` → `(K12)`: a label that did not come from this turn's searches cannot resolve as one."""
    return _LABEL.sub(r"(K\1)", text or "")


def sanitize(text: str) -> str:
    """Document text made safe to place between the engine's markers.

    It cannot close a `<<…>>` block, carry a `[K#]` label, or start a line the
    engine's own transcript lines start with (`[Turn N — …]`, `System:`).
    """
    text = (text or "").replace("<<", "‹‹").replace(">>", "››")
    return _TURN_LINE.sub(r" \1", neutralize(text))


_RUN = r"\[K\d{1,3}\](?:[ \t]*[,;]?[ \t]*\[K\d{1,3}\])*"
_PAREN_RUN = re.compile(rf"[ \t]*\([ \t]*{_RUN}[ \t]*\)")
_LEADING_RUN = re.compile(rf"(?m)^{_RUN}[ \t]?")
_INLINE_RUN = re.compile(rf"[ \t]*{_RUN}")


def strip_labels(text: str) -> str:
    """The answer without its `[K#]` labels, for a surface that cannot show citations."""
    for pattern in (_PAREN_RUN, _LEADING_RUN, _INLINE_RUN):
        text = pattern.sub("", text or "")
    return text


# ── the labels of one turn ─────────────────────────────────────────────────────

@dataclass
class LabelEntry:
    """What one label names: a chunk, and how it was shown."""

    chunk_id: str
    kb_id: str
    #: How search showed it: "" (the chunk), "n<w>" (with w neighbours each side) or
    #: "s<tokens>" (its section) — enough to rebuild the text after a resume.
    expansion: str = ""
    #: The windows `read_knowledge` showed (the last MAX_READS).
    reads: list[int] = field(default_factory=list)
    #: Live only, never in the snapshot: the resolved citation, and the exact texts shown.
    citation: dict | None = None
    shown: list[str] = field(default_factory=list)


class KnowledgeTurn:
    """The `[K#]` labels of one root turn and its delegates."""

    def __init__(self) -> None:
        self.next_label = 1
        self.labels: dict[int, LabelEntry] = {}

    def __bool__(self) -> bool:
        return bool(self.labels)

    def start(self) -> int:
        """The first label the next search may use."""
        if self.next_label > MAX_LABEL:
            raise KnowledgeError("This answer has used every label ([K1]–[K999]).", code="labels_exhausted")
        return self.next_label

    def record(self, n: int, *, chunk_id: str, kb_id: str, expansion: str = "", citation: dict | None = None,
               shown: str | None = None) -> None:
        self.labels[n] = LabelEntry(chunk_id=chunk_id, kb_id=kb_id, expansion=expansion, citation=citation,
                                    shown=[shown] if shown else [])
        self.next_label = max(self.next_label, n + 1)

    def record_read(self, n: int, window: int, text: str) -> None:
        entry = self.labels.get(n)
        if entry is None:
            return
        entry.reads = [w for w in entry.reads if w != window][-(MAX_READS - 1):] + [window]
        entry.shown.append(text)

    def entry(self, n: int) -> LabelEntry | None:
        return self.labels.get(n)

    # ── the snapshot ──

    def to_state(self) -> dict | None:
        """What a paused or crashed turn keeps (about 100 bytes a label); None when there is nothing."""
        if not self.labels:
            return None
        labels: dict[str, dict] = {}
        for n, e in sorted(self.labels.items()):
            item: dict[str, Any] = {"c": e.chunk_id, "k": e.kb_id}
            if e.expansion:
                item["x"] = e.expansion
            if e.reads:
                item["r"] = list(e.reads)
            labels[str(n)] = item
        return {"v": STATE_VERSION, "next": self.next_label, "labels": labels}

    @classmethod
    def from_state(cls, state: Any) -> KnowledgeTurn:
        """A turn from its snapshot. Anything that does not read is dropped (and logged), never raised."""
        turn = cls()
        if not state:
            return turn
        if not isinstance(state, dict) or not isinstance(state.get("labels"), dict):
            _log.warning("knowledge labels in a resumed turn's snapshot do not read; starting afresh")
            return turn
        for key, item in state["labels"].items():
            try:
                n = int(key)
            except (TypeError, ValueError):
                continue
            if not (1 <= n <= MAX_LABEL) or not isinstance(item, dict):
                continue
            chunk_id, kb_id = item.get("c"), item.get("k")
            if not (isinstance(chunk_id, str) and isinstance(kb_id, str)):
                continue
            reads = [w for w in item.get("r") or [] if isinstance(w, int) and 0 <= w <= 3][-MAX_READS:]
            turn.labels[n] = LabelEntry(chunk_id=chunk_id, kb_id=kb_id, expansion=str(item.get("x") or ""),
                                        reads=reads)
        stated = state.get("next")
        turn.next_label = max(stated if isinstance(stated, int) else 1, max(turn.labels, default=0) + 1)
        return turn

    def merge(self, other: KnowledgeTurn) -> None:
        """Take in `other`'s labels (a delegate resumed from its own snapshot beside the lead's turn)."""
        for n, theirs in other.labels.items():
            mine = self.labels.get(n)
            if mine is None:
                self.labels[n] = theirs
            elif mine.chunk_id == theirs.chunk_id:
                mine.reads = [w for w in mine.reads if w not in theirs.reads] + theirs.reads
                mine.reads = mine.reads[-MAX_READS:]
            else:
                _log.warning(f"knowledge label [K{n}] names two chunks after a resume; keeping {mine.chunk_id}")
        self.next_label = max(self.next_label, other.next_label)


# ── the quote check ────────────────────────────────────────────────────────────

#: A double-quoted span on one line, then up to three characters of space or
#: punctuation, then one to five labels: `"exact words" [K3]`, `“…” ([K1][K2])`.
_QUOTED = re.compile(
    r'["“](?P<q>[^"“”\n]{1,1500})["”][\s,.;:(]{0,3}(?P<labels>\[K\d{1,3}\](?:[ \t]*[,;]?[ \t]*\[K\d{1,3}\]){0,4})')
_ELLIPSIS = re.compile(r"\s*(?:…|\.\.\.|\[…\]|\[\.\.\.\]|\(…\)|\(\.\.\.\))\s*")
_LINE_HYPHEN = re.compile(r"(\w)-\n(\w)")
_SPACE = re.compile(r"\s+")
_FOLD = str.maketrans({
    **{c: "'" for c in "‘’‚‛′´"},
    **{c: '"' for c in "“”„‟″«»"},
    **{c: "-" for c in "‐‑‒–—―−﹘﹣－"},
    **{c: None for c in "­​‌‍⁠﻿*`|"},
})
_EDGE = " \t.,;:!?\"'"


def normalize(text: str) -> str:
    """Case, quote marks, dashes, invisible characters, markdown emphasis, table pipes and
    whitespace folded, so a quote copied faithfully matches whatever the page did with them."""
    text = _LINE_HYPHEN.sub(r"\1\2", text or "")
    text = unicodedata.normalize("NFKC", text).casefold().translate(_FOLD)
    return _SPACE.sub(" ", text).strip()


def _in_order(parts: list[str], text: str) -> bool:
    at = 0
    for part in parts:
        found = text.find(part, at)
        if found < 0:
            return False
        at = found + len(part)
    return True


def check_quote(quote: str, references: list[str]) -> dict | None:
    """`{quote, matched, method}` for `quote` against the texts shown under its label(s);
    None when it is too short to be a quote (a term, not a passage)."""
    from core.knowledge.chunker import estimate_tokens

    if estimate_tokens(normalize(quote)) < 3:
        return None
    raw = [p.strip() for p in _ELLIPSIS.split(quote) if p.strip()]
    if not raw:
        return None
    shown = quote.strip()[:QUOTE_MAX]
    if any(_in_order(raw, ref) for ref in references):
        return {"quote": shown, "matched": True, "method": "exact"}
    folded = [p for p in (normalize(p).strip(_EDGE) for p in raw) if p]
    matched = bool(folded) and any(_in_order(folded, normalize(ref)) for ref in references)
    return {"quote": shown, "matched": matched, "method": "normalized"}


def quotes_in(answer: str) -> list[tuple[str, list[int]]]:
    """Each quote the answer attributes to labels, with those labels, in order."""
    found = []
    for match in _QUOTED.finditer(answer or ""):
        labels = [int(n) for n in _LABEL.findall(match.group("labels"))]
        if labels:
            found.append((match.group("q"), labels))
    return found


# ── citations ──────────────────────────────────────────────────────────────────

async def _rebuild(turn: KnowledgeTurn, numbers: list[int]) -> None:
    """Citations and shown texts for labels a resume left without them, from the chunk rows.
    A chunk that is gone (its document was indexed again since) stays without a citation."""
    from core.knowledge import search

    views = await search.load_chunks(sorted({turn.labels[n].chunk_id for n in numbers}))
    for n in numbers:
        entry = turn.labels[n]
        view = views.get(entry.chunk_id)
        if view is None:
            continue
        if entry.citation is None:
            entry.citation = search.citation(format_label(n), view).model_dump(mode="json")
        if entry.shown:
            continue
        row = view.row
        if entry.expansion.startswith("n") and entry.expansion[1:].isdigit():
            text = search.joined(await search.neighbours(row, int(entry.expansion[1:])))
        elif entry.expansion.startswith("s") and entry.expansion[1:].isdigit():
            text = search.joined(await search.section(row, int(entry.expansion[1:])))
        else:
            text = row.content
        context = f"{row.context}\n" if row.context else ""
        entry.shown.append(sanitize(context + text))
        for window in entry.reads:
            entry.shown.append(sanitize(search.joined(await search.neighbours(row, window))))


async def citations_event(answer: str, turn: KnowledgeTurn) -> dict:
    """The `knowledge_citations` event for `answer`: each label it uses, resolved, with the first
    failing quote check (else the first check), and the labels no search in this turn produced."""
    from core.knowledge.api_models import Citation

    used = labels_in(answer)
    missing = [n for n in used if n in turn.labels
               and (turn.labels[n].citation is None or not turn.labels[n].shown)]
    if missing:
        await _rebuild(turn, missing)

    checks: dict[int, list[dict]] = {}
    for quote, numbers in quotes_in(answer):
        references = [text for n in numbers if n in turn.labels for text in turn.labels[n].shown]
        check = check_quote(quote, references) if references else None
        if check is not None:
            for n in numbers:
                checks.setdefault(n, []).append(check)

    citations, unresolved = [], []
    for n in used:
        entry = turn.labels.get(n)
        if entry is None or entry.citation is None:
            unresolved.append(format_label(n))
            continue
        done = checks.get(n) or []
        check = next((c for c in done if not c["matched"]), done[0] if done else None)
        citations.append(Citation.model_validate({**entry.citation, "label": format_label(n),
                                                  "quote_check": check}).model_dump(mode="json"))
    return {"type": "knowledge_citations", "citations": citations, "unresolved": unresolved}
