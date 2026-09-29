"""
Questions, drawn in the screen's question area while the steps keep moving.

Each widget returns its answer, or `BACK` when the user pressed esc and the
caller offered a way back. With no keyboard (no terminal, `--yes`) each one
returns its default without drawing anything — the caller decides what the
default is, so a non-interactive install is the same flow with no questions.
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from dataclasses import dataclass

from .screen import _Screen
from .tui import Keys

BACK = object()


class Cancelled(Exception):
    """The install failed while a question was open; the answer no longer matters."""


@dataclass
class Option:
    label: str
    value: object = None
    hint: str = ""


class Asker:
    """Draws questions on `screen`, reads answers from `keys` (None: defaults)."""

    def __init__(self, screen: _Screen, keys: Keys | None,
                 cancel: threading.Event | None = None):
        self.screen = screen
        self.keys = keys
        self.term = screen.term
        self.cancel = cancel

    def _read(self) -> str:
        """The next key — or `Cancelled`, the moment a background step fails."""
        while True:
            if self.cancel is not None and self.cancel.is_set():
                self.screen.set_panel([])
                raise Cancelled
            if self.keys.ready(0.1):
                return self.keys.read()

    @property
    def interactive(self) -> bool:
        return self.keys is not None

    # ── select ─────────────────────────────────────────────────────────────
    def select(self, question: str, options: list[Option], default: int = 0,
               back: bool = False, filterable: bool = False, note: str = "",
               window: int = 8):
        """One of `options`. ↑↓ move, enter picks, a digit jumps, typing filters."""
        if not self.interactive:
            return options[default].value
        t, g = self.term, self.term.g
        index, query = default, ""
        with self.keys.raw():
            while True:
                shown = [o for o in options
                         if not query or query.lower() in (o.label + " " + o.hint).lower()]
                index = min(index, max(0, len(shown) - 1))
                top = max(0, min(index - window // 2, len(shown) - window))
                lines = ["  " + t.style(question, "bold")]
                lines += ["  " + t.style(n, "dim") for n in note.split("\n") if note]
                for i, o in enumerate(shown[top:top + window], start=top):
                    mark = t.style(g("pointer"), "cyan") if i == index else " "
                    label = t.style(o.label, "cyan") if i == index else o.label
                    hint = f"  {t.style(o.hint, 'dim')}" if o.hint else ""
                    lines.append(f"  {mark} {label}{hint}")
                if not shown:
                    lines.append("    " + t.style("nothing matches", "dim"))
                elif len(shown) > window:
                    lines.append("    " + t.style(f"{len(shown)} options", "dim"))
                keys_hint = f"↑↓ move {g('dot')} enter select"
                if filterable:
                    keys_hint += f" {g('dot')} type to filter" + (f": {query}" if query else "")
                if back:
                    keys_hint += f" {g('dot')} esc back"
                lines.append("  " + t.style(keys_hint if g("dot") == "·" else keys_hint.replace("↑↓", "up/down"), "dim"))
                self.screen.set_panel(lines)

                key = self._read()
                if key == "up":
                    index = (index - 1) % max(1, len(shown))
                elif key == "down":
                    index = (index + 1) % max(1, len(shown))
                elif key == "enter" and shown:
                    self.screen.set_panel([])
                    return shown[index].value
                elif key == "esc":
                    if query:
                        query = ""
                    elif back:
                        self.screen.set_panel([])
                        return BACK
                elif key == "backspace":
                    query = query[:-1]
                elif len(key) == 1 and key.isdigit() and not query and 0 < int(key) <= len(shown):
                    index = int(key) - 1
                elif filterable and len(key) == 1 and key.isprintable():
                    query += key
                    index = 0

    def confirm(self, question: str, default: bool = True, note: str = "",
                yes: str = "Yes", no: str = "No") -> bool:
        options = [Option(yes, True), Option(no, False)]
        return self.select(question, options, default=0 if default else 1, note=note)

    # ── text ───────────────────────────────────────────────────────────────
    def text(self, question: str, default: str = "", secret: bool = False,
             note: str = "", back: bool = True, validate=None):
        """A line of text. `validate(value)` returns an error message or None."""
        if not self.interactive:
            return default
        t, g = self.term, self.term.g
        value, error = "", ""
        with self.keys.raw():
            while True:
                shown = g("secret") * min(len(value), 32) if secret else value
                placeholder = t.style(default, "dim") if (not value and default and not secret) else ""
                lines = ["  " + t.style(question, "bold")]
                lines += ["  " + t.style(n, "dim") for n in note.split("\n") if note]
                lines.append(f"  {t.style(g('pointer'), 'cyan')} {shown}{placeholder}")
                if error:
                    lines.append("  " + t.style(error, "yellow"))
                hint = "enter confirm" + (f" {g('dot')} esc back" if back else "")
                lines.append("  " + t.style(hint, "dim"))
                self.screen.set_panel(lines)

                key = self._read()
                if key == "enter":
                    answer = value.strip() or default
                    problem = validate(answer) if validate else None
                    if problem:
                        error = problem
                        continue
                    self.screen.set_panel([])
                    return answer
                if key == "esc" and back:
                    self.screen.set_panel([])
                    return BACK
                if key == "backspace":
                    value = value[:-1]
                elif len(key) == 1 and key.isprintable():
                    value += key
                    error = ""

    def secret(self, question: str, note: str = "", back: bool = True, validate=None):
        return self.text(question, secret=True, note=note, back=back, validate=validate)

    # ── something happening ────────────────────────────────────────────────
    @contextmanager
    def busy(self, text: str):
        """A spinner line in the question area while a slow call runs."""
        if not self.interactive:
            yield
            return
        stop = threading.Event()
        g = self.term.g

        def spin():
            i = 0
            while not stop.wait(0.08):
                frame = g("spin")[i % len(g("spin"))]
                self.screen.set_panel([f"  {self.term.style(frame, 'cyan')} {text}"])
                i += 1

        thread = threading.Thread(target=spin, daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join()
            self.screen.set_panel([])

    def say(self, lines: list[str]) -> None:
        """Leave a note in the question area (not a question)."""
        if self.interactive:
            self.screen.set_panel(["  " + line for line in lines])
