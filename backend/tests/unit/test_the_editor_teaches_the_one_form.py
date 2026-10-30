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

# A single-line string literal: '…', "…" or `…`.
LITERAL = re.compile(r"'(?:[^'\\\n]|\\.)*'|\"(?:[^\"\\\n]|\\.)*\"|`(?:[^`\\]|\\.)*`")
OLD_FORM = re.compile(r"(?<!\{)\{state\.[A-Za-z_][\w.]*\}(?!\})")


def _code_lines(text: str) -> str:
    """The file without its comment lines (`//`, `/*`, ` *`), where the older form is named on purpose."""
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith(("//", "/*", "*")))


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
        for literal in LITERAL.findall(_code_lines(path.read_text("utf-8"))):
            if OLD_FORM.search(literal):
                found.append(f"{path.relative_to(SRC)}: {literal[:80]}")
    assert not found, "strings teach `{state.x}`, not `{{ state.x }}`:\n" + "\n".join(found)
