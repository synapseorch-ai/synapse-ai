"""How the CLI looks at a terminal: Synapse's colour, its mark, a working line, a question's box (D294).

Naveen (2026-10-17): a chat and a run at the terminal should read as Claude Code's and Codex's do, in Synapse's
theme. The accent is the product's action colour (`--accent`, tokens.css: #45cffc on dark), kept to marks, rules and
borders so the words stay readable on a light terminal too. The mark is the brand's rings (`◉`); the spinner is a
signal going round them. Only a terminal sees any of this: piped or with --json, the CLI prints plain lines.
"""
from __future__ import annotations

import shutil
import sys
import time

from rich.console import Console, Group, RenderableType
from rich.markdown import Markdown
from rich.padding import Padding
from rich.panel import Panel
from rich.text import Text

ACCENT = "#45cffc"
MARK = "◉"
DOT = "●"
NEST = "⎿"
_RINGS = {"interval": 110, "frames": ["◐", "◓", "◑", "◒"]}

try:  # the spinner is the brand's signal going round its rings
    from rich import _spinners

    _spinners.SPINNERS.setdefault("synapse", _RINGS)
    SPINNER = "synapse"
except Exception:  # noqa: BLE001 — a rich without the table draws its own dots
    SPINNER = "dots"


class Working:
    """`◐ Thinking… 3s · Ctrl-C to stop`: what is being done, for how long, and how to stop it."""

    def __init__(self, what: str = "Thinking…", hint: str = "Ctrl-C to stop"):
        self.what = what
        self.hint = hint
        self.started = time.monotonic()

    def __rich__(self) -> RenderableType:
        # Drawn again on every tick of the status's own spinner, so the seconds count up.
        took = int(time.monotonic() - self.started)
        line = Text(self.what)
        line.append(f"  {took}s · {self.hint}", style="dim")
        return line


def working(console: Console, what: str = "Thinking…"):
    """A live working line on `console`, with `.update(what)`: stopped with `.stop()`."""
    state = Working(what)
    status = console.status(state, spinner=SPINNER, spinner_style=ACCENT)

    class _Handle:
        def start(self):
            status.start()
            return self

        def update(self, what: str) -> None:
            state.what = what
            status.update(state)

        def stop(self) -> None:
            status.stop()

    return _Handle().start()


def welcome(console: Console, title: str, rows: list[tuple[str, str]], hint: str) -> None:
    """The box a session opens with, as Claude Code's and Codex's do: the mark, what is open, how to use it."""
    body = Text()
    body.append(f"{MARK} ", style=f"bold {ACCENT}")
    body.append(title, style="bold")
    body.append("\n")
    width = max((len(k) for k, _ in rows), default=0)
    for key, value in rows:
        if value:
            body.append(f"\n  {key.ljust(width)}  ", style="dim")
            body.append(value)
    body.append(f"\n\n  {hint}", style="dim")
    console.print(Panel(body, border_style=ACCENT, padding=(0, 1), expand=False))


def said(console: Console, who: str, text: str, *, style: str = ACCENT) -> None:
    """A block of words under a dot and a name: an agent's answer, a run's."""
    head = Text()
    head.append(f"{DOT} ", style=style)
    head.append(who, style="bold")
    console.print(head)
    console.print(Padding(Markdown(text or ""), (0, 0, 0, 2)))


def call_line(name: str, args: dict | None) -> Text:
    """`● web_search(query: "…")`: a tool call, its arguments in short."""
    line = Text()
    line.append(f"{DOT} ", style="dim")
    line.append(name, style="bold")
    shown = ", ".join(f"{k}: {_short(v)}" for k, v in (args or {}).items())
    if shown:
        line.append(f"({shown[:100]}{'…' if len(shown) > 100 else ''})", style="dim")
    return line


def result_line(preview: str) -> Text:
    """`  ⎿ 3 results`: the first line of what a call answered."""
    first = next((ln.strip() for ln in str(preview or "").splitlines() if ln.strip()), "done")
    return Text(f"  {NEST} {first[:110]}", style="dim")


def _short(value) -> str:
    import json

    text = value if isinstance(value, str) else json.dumps(value, default=str)
    text = " ".join(str(text).split())
    return f'"{text[:40]}…"' if len(text) > 40 else (f'"{text}"' if isinstance(value, str) else text)


def question_box(console: Console, title: str, lines: list[str], *, border: str = "yellow") -> None:
    """A question that waits on the person here: boxed, as Claude Code asks before a tool runs."""
    body = Group(*(Text(line) for line in lines)) if lines else Text("")
    console.print(Panel(body, title=Text(title, style="bold"), title_align="left", border_style=border,
                        padding=(0, 1), expand=False))


def composer_rule(console: Console) -> None:
    """The thin line the person types under."""
    console.print(Text("─" * min(console.width, 100), style="dim"))


def echo_message(console: Console, message: str, typed_lines: int) -> None:
    """Redraw what the person typed as a quiet block in place of the rule and the raw input (Claude Code's look)."""
    stream = console.file
    width = max(20, shutil.get_terminal_size((100, 20)).columns)
    # The rule, and each typed line, wrapped at the terminal's width with its 2-character prompt.
    rows = 1 + sum(max(1, -(-(len(line) + 2) // width)) for line in (message.split("\n") or [""]))
    rows = max(rows, typed_lines)
    stream.write("\x1b[1A\x1b[2K" * rows)
    stream.flush()
    block = Text()
    block.append("› ", style=f"bold {ACCENT}")
    block.append(message)
    console.print(Padding(block, (0, 1), style="on grey15") if console.color_system else block)


def is_terminal(console: Console) -> bool:
    return console.is_terminal and sys.stdin.isatty()
