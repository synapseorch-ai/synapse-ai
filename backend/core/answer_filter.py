"""What of a model call's streamed text a person may see while it arrives (U72).

A streaming call hands the engine raw text: the answer, but also reasoning in
`[REASONING]…[/REASONING]` or `<think>…</think>`, and tool calls written as JSON
(bare, in a ```json fence, or in `<tool_call>`). The engine strips and parses all
of that once the call has ended. A stream cannot wait that long, so this filter
decides, piece by piece, what is certainly answer text:

* reasoning blocks are hidden, and a marker split across two pieces is held
  until it is whole;
* a closing `</think>` with no opening one means everything before it was
  thinking (a template that opened the tag for the model), so what went out is
  taken back: a reset, then the text after it;
* JSON is held from its `{` until the object closes. A tool call (any object
  with a `tool` key, as `react_engine._is_tool_call` reads it) stops the call's
  stream for good; any other object is released. A `{` that does not open a
  JSON object (`{name}`, code) streams as text;
* a fence streams unless its first line opens with `{`, and is held to its
  closing fence then, as JSON is;
* `<tool_call` stops the call's stream for good.

Prose before a tool call does stream: the engine files it as reasoning once the
call ends, and says so with an `answer_reset` (`reason="tool_call"`).

The filter recomputes the visible text of everything the call has sent on each
feed and emits only what is new, which keeps it simple and correct for the
retroactive cases. A call's text is short enough for that (tens of KB at most,
fed once per waiter wake, not once per token).
"""
from __future__ import annotations

import json
import re

#: Every marker that hides or reveals text, as one tokenizer.
_MARKER_RE = re.compile(r"\[REASONING\]|\[/REASONING\]|</?think(?:ing)?>", re.IGNORECASE)
#: Markers a held tail may be the start of.
_MARKERS = ("[reasoning]", "[/reasoning]", "<think>", "<thinking>", "</think>", "</thinking>",
            "<tool_call", "```")
_JSON = json.JSONDecoder()

Op = tuple  # ("text", str) | ("reset", reason)


def _partial_marker_tail(raw: str) -> int:
    """Length of a suffix of `raw` that could still become a marker."""
    low = raw[-12:].lower()
    if any(low.endswith(m) for m in _MARKERS):
        return 0   # the tail is a whole marker ("```" ending a fence), not the start of one
    for size in range(min(len(low), 11), 0, -1):
        tail = low[-size:]
        if any(m.startswith(tail) and m != tail for m in _MARKERS):
            return size
    return 0


def _strip_reasoning(raw: str) -> tuple[str, bool]:
    """The visible text of `raw`, and whether an orphan `</think>` voided what came before it."""
    out: list[str] = []
    hidden: str | None = None   # "reasoning" | "think"
    voided = False
    pos = 0
    for m in _MARKER_RE.finditer(raw):
        token = m.group(0).lower()
        if hidden is None:
            out.append(raw[pos:m.start()])
        pos = m.end()
        if token == "[reasoning]":
            hidden = hidden or "reasoning"
        elif token == "[/reasoning]":
            if hidden == "reasoning":
                hidden = None
        elif token.startswith("</"):
            if hidden == "think":
                hidden = None
            elif hidden is None:
                # Closed without opening: all of it before was thinking.
                out = []
                voided = True
        else:
            hidden = hidden or "think"
    if hidden is None:
        out.append(raw[pos:])
    return "".join(out), voided


def _is_tool_call(obj) -> bool:
    return isinstance(obj, dict) and isinstance(obj.get("tool"), str) and bool(obj["tool"].strip())


def _json_object_at(text: str, pos: int) -> tuple[str, int]:
    """At a `{`: ("open", -) undecided, ("tool", -), ("json", end) or ("text", -)."""
    rest = text[pos + 1:].lstrip()
    if not rest:
        return "open", 0
    if rest[0] not in "\"'}":
        return "text", 0
    try:
        obj, end = _JSON.raw_decode(text[pos:])
    except json.JSONDecodeError:
        # Unfinished, or never JSON at all: held while it might still close.
        return ("open", 0) if _might_close(text[pos:]) else ("text", 0)
    if _is_tool_call(obj):
        return "tool", 0
    return "json", pos + end


def _might_close(fragment: str) -> bool:
    """False once the braces of `fragment` balanced and it still was not JSON."""
    depth, in_str, esc = 0, None, False
    for ch in fragment:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == in_str:
                in_str = None
        elif ch in "\"'":
            in_str = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return False
    return True


def _visible(raw: str, *, final: bool) -> tuple[str, bool, bool]:
    """(the releasable answer text, voided by an orphan close, a tool call seen)."""
    if not final:
        tail = _partial_marker_tail(raw)
        if tail:
            raw = raw[:-tail]
    text, voided = _strip_reasoning(raw)
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "{":
            kind, end = _json_object_at(text, i)
            if kind == "tool":
                return text[:i], voided, True
            if kind == "open":
                return (text if final else text[:i]), voided, False
            i = end if kind == "json" else i + 1
            continue
        if text.startswith("<tool_call", i) or text[i:i + 10].lower() == "<tool_call":
            return text[:i], voided, True
        if text.startswith("```", i):
            line_end = text.find("\n", i)
            if line_end < 0:
                return (text if final else text[:i]), voided, False
            body = text[line_end + 1:]
            stripped = body.lstrip()
            if not stripped:
                return (text if final else text[:i]), voided, False
            if stripped[0] != "{":
                i = line_end + 1
                continue
            close = text.find("```", line_end + 1)
            if close < 0:
                return (text if final else text[:i]), voided, False
            try:
                obj, _ = _JSON.raw_decode(text[line_end + 1:close].strip())
            except json.JSONDecodeError:
                obj = None
            if _is_tool_call(obj):
                return text[:i], voided, True
            i = close + 3
            continue
        i += 1
    return text, voided, False


class AnswerFilter:
    """One model call's streamed text in, the answer's visible pieces out.

    `feed()` and `close()` return a list of operations: `("text", s)` to show, or
    `("reset", reason)` to take back what was shown so far. `close()` is called
    only once the engine knows the call was the answer, and releases what was
    held while it might have been a tool call.
    """

    def __init__(self) -> None:
        self._raw = ""
        self._shown = ""
        self._stopped = False
        self._voided = False

    @property
    def shown(self) -> str:
        """Everything shown since the last reset."""
        return self._shown

    @property
    def stopped(self) -> bool:
        """True once a tool call was seen: nothing more is shown for this call."""
        return self._stopped

    def feed(self, piece: str) -> list[Op]:
        self._raw += piece
        return self._update(final=False)

    def close(self) -> list[Op]:
        return self._update(final=True)

    def restart(self) -> list[Op]:
        """The call is being sent again: everything it sent is void."""
        had = bool(self._shown)
        self._raw = self._shown = ""
        self._stopped = self._voided = False
        return [("reset", "retry")] if had else []

    def _update(self, *, final: bool) -> list[Op]:
        if self._stopped:
            return []
        text, voided, tool = _visible(self._raw, final=final)
        text = text.lstrip()
        self._stopped = tool
        ops: list[Op] = []
        if voided and not self._voided:
            self._voided = True
            if self._shown:
                ops.append(("reset", "think_close"))
                self._shown = ""
        if not text.startswith(self._shown):
            # Never expected (decisions only ever extend the text), but a
            # stream that disagrees with itself is taken back, never garbled.
            ops.append(("reset", "think_close"))
            self._shown = ""
        new = text[len(self._shown):]
        if new:
            self._shown = text
            ops.append(("text", new))
        return ops
