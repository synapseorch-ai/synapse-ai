"""What a command prints: rich for a person, JSON for a script.

With `--json`, stdout carries only JSON — a document for `list`/`show`, one
event per line for a stream — and anything meant for a person goes to stderr.
Without it, results go to stdout and progress to stderr, so `synapse run x >
out.txt` keeps only the answer.
"""
from __future__ import annotations

import json
import sys
from collections.abc import Callable, Iterable

from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table


class Output:
    def __init__(self, json_mode: bool = False):
        self.json_mode = json_mode
        # Piped, rich would squeeze a table into 80 columns; `| grep` wants whole lines.
        self.out = Console(highlight=False, width=None if sys.stdout.isatty() else 400)
        # A note is never wrapped: it often ends in a command to copy.
        self.err = Console(stderr=True, highlight=False, soft_wrap=True)

    # ── data ────────────────────────────────────────────────────────────────

    def document(self, data) -> None:
        """A whole API response, as the API sent it."""
        sys.stdout.write(json.dumps(data, indent=2, default=str) + "\n")
        sys.stdout.flush()

    def event(self, data) -> None:
        """One streamed event, one line."""
        sys.stdout.write(json.dumps(data, default=str) + "\n")
        sys.stdout.flush()

    def table(self, rows: Iterable[dict], columns: list[tuple[str, Callable[[dict], object]]],
              empty: str) -> None:
        rows = list(rows)
        if not rows:
            self.err.print(empty)
            return
        table = Table(box=None, header_style="bold", pad_edge=False)
        for i, (header, _) in enumerate(columns):
            # The first column is the id a person copies into the next command.
            table.add_column(header, overflow="fold", no_wrap=(i == 0))
        for row in rows:
            table.add_row(*("" if (v := fn(row)) is None else str(v) for _, fn in columns))
        self.out.print(table)

    def fields(self, pairs: list[tuple[str, object]]) -> None:
        """A record as `label  value` lines, blanks left out."""
        width = max(len(k) for k, _ in pairs)
        for label, value in pairs:
            if value in (None, "", []):
                continue
            self.out.print(f"[bold]{label.ljust(width)}[/bold]  {value}", markup=True)

    def markdown(self, text: str) -> None:
        if self.out.is_terminal:
            self.out.print(Markdown(text or ""))
        else:
            sys.stdout.write((text or "") + "\n")
            sys.stdout.flush()

    def text(self, text: str) -> None:
        self.out.print(text, markup=False)

    # ── for a person ────────────────────────────────────────────────────────

    def note(self, message: str, style: str = "dim") -> None:
        self.err.print(message, style=style, markup=False)

    def error(self, message: str) -> None:
        self.err.print(f"Error: {message}", style="red", markup=False)
