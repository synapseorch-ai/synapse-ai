"""What the editor tells a person to type for the run's state is the one form, `{{ state.key }}` (F1252; D3ASM's find).

F1252 made `{{ state.x }}` the one form and writes an older `{state.x}` that way when a step is saved, but the
Browser step's input hint still said "{state.key} or text", so the screen taught the form the release notes say is
gone. Any string the frontend shows (a quoted or template literal, not a comment) is held to the one form here; the
engine's own reading of the older form is `test_a_template_has_one_form.py`'s.
"""
from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[3] / "frontend" / "src"
STEPS = SRC / "components" / "orchestration" / "panel" / "steps.tsx"

# Not `${state.x}`: that is a template literal's own interpolation, code, never text.
OLD_FORM = re.compile(r"(?<![{$])\{state\.[A-Za-z_][\w.]*\}(?!\})")


def _code_lines(text: str) -> list[str]:
    """The file's lines but its comment lines (`//`, `/*`, ` *`), where the older form is named on purpose."""
    return [l for l in text.splitlines() if not l.lstrip().startswith(("//", "/*", "*"))]


def _in_a_string(line: str, at: int) -> bool:
    """Whether `at` is inside a '…', "…" or `…` opened on this line: a bare JSX `{state.x}` is code, the same
    words inside quotes are text a person reads. Read line by line, so one stray quote never shifts a whole file."""
    quote, i = None, 0
    while i < at:
        c = line[i]
        if quote and c == "\\":
            i += 2
            continue
        if quote and c == quote:
            quote = None
        elif not quote and c in "'\"`":
            quote = c
        i += 1
    return quote is not None


def test_the_browser_steps_input_hint_names_the_one_form():
    text = STEPS.read_text("utf-8")
    hint = re.search(r"hint=\{`What it types for \{\$\{name\}\}, from the run: ([^`]*)`\}", text)
    assert hint, "the Browser step's input hint is not where it was"
    assert hint.group(1) == "{{ state.key }} or text."


def test_no_string_the_frontend_shows_teaches_the_older_form():
    found = []
    for path in sorted(SRC.rglob("*")):
        if path.suffix not in (".ts", ".tsx"):
            continue
        for line in _code_lines(path.read_text("utf-8")):
            if any(_in_a_string(line, m.start()) for m in OLD_FORM.finditer(line)):
                found.append(f"{path.relative_to(SRC)}: {line.strip()[:100]}")
    assert not found, "strings teach `{state.x}`, not `{{ state.x }}`:\n" + "\n".join(found)
