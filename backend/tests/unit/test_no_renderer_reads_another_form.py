"""Every renderer reads the one form, and nothing shipped writes another (F1252 stage B; Naveen 10-30).

`{{ state.x }}` is the only reference to state (`templating.STATE_REF`); an older `{state.x}` is written so as a
definition is read (`templating.one_form`, stage A). So each renderer reads that form alone, spaced or not: a model's
prompt, a Human prompt, a Print, a Tool step's set arguments, a Browser step's inputs and a Retrieve query. And what we
ship never teaches the other: the builder's bundle, its prompts and help, the example packs, the code that writes a
template or a hint, and the editor's own words. A walker keeps each of those so, and another keeps every module from
compiling its own pattern for a reference to state: that is how a renderer drifted to one form while the planner
wrote the other.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from core.orchestration.context import _fill_task_template
from core.orchestration.steps import render_state_refs, render_tool_arguments
from core.templating import _OLD_STATE_REF

BACKEND = Path(__file__).resolve().parents[2]
REPO = BACKEND.parent
STATE = {"user_input": "HP-2211", "order": {"id": "A7", "city": "Pune"}, "post": '{"text": "Hello"}'}


def _prompt(text: str) -> str:
    return _fill_task_template(text, STATE, {"API_KEY": "sk-not-real"})


def _person(text: str) -> str:
    return render_state_refs(text, STATE)


def _tool(text: str) -> str:
    return render_tool_arguments({"q": text}, STATE)["q"]


RENDERERS = {"a model's prompt (agent, llm, tool, Browser input, Retrieve query)": _prompt,
             "a person's text (Human prompt, Print)": _person, "a Tool step's set argument": _tool}


@pytest.mark.parametrize("render", RENDERERS.values(), ids=RENDERERS.keys())
@pytest.mark.parametrize("ref", ["{{state.order.city}}", "{{ state.order.city }}", "{{  state.order.city  }}"])
def test_each_renderer_reads_the_one_form_spaced_or_not(render, ref):
    assert render(f"Ship to {ref}.") == "Ship to Pune."


@pytest.mark.parametrize("render", RENDERERS.values(), ids=RENDERERS.keys())
def test_each_reads_a_field_of_json_text_alike(render):
    assert render("{{ state.post.text }}") == "Hello"


@pytest.mark.parametrize("render", [_prompt, _person], ids=["prompt", "person"])
def test_no_renderer_reads_the_older_form_raw(render):
    """The converter writes it as the one form before any renderer sees it; raw, it is just text."""
    assert render("{state.user_input}") == "{state.user_input}"


def test_a_persons_text_never_fills_a_global():
    """A Human prompt can go to a channel (D104): a workspace variable is never filled into a person's text."""
    assert _person("Key: {{ global.API_KEY }}") == "Key: {{ global.API_KEY }}"
    assert _prompt("Key: {{ global.API_KEY }}") == "Key: sk-not-real", "a model's prompt does"


# ── what we ship ──────────────────────────────────────────────────────────────────────────────────────────────────

#: Where the older form is named on purpose: the converter, and the two notes that say it is converted.
NAMES_IT = {"core/templating.py", "core/models_orchestration.py", "core/orchestration/context.py"}
#: An f-string that writes the older form (`f"{{state.{key}}}"`).
_OLD_FSTRING = re.compile(r"(?<!\{)\{\{state\.\{")


def _shipped_text() -> list[Path]:
    native = BACKEND / "core" / "native_builder"
    return [*native.glob("*.json"), *(native / "agents").glob("*.json"), *(native / "prompts").glob("*.md"),
            *(BACKEND / "examples").glob("*.json")]


def _sources() -> list[Path]:
    return [p for root in ("core", "tools") for p in (BACKEND / root).rglob("*.py")]


@pytest.mark.parametrize("path", _shipped_text(), ids=lambda p: p.name)
def test_no_shipped_definition_prompt_or_help_holds_the_older_form(path):
    text = path.read_text()
    json.loads(text) if path.suffix == ".json" else None
    assert not _OLD_STATE_REF.findall(text), _OLD_STATE_REF.findall(text)


def test_no_code_writes_the_older_form():
    found = []
    for path in _sources():
        rel = str(path.relative_to(BACKEND))
        text = path.read_text()
        if rel not in NAMES_IT:
            found += [f"{rel}: {m.group(0)}" for m in _OLD_STATE_REF.finditer(text)]
        found += [f"{rel}: an f-string writing {m.group(0)}…" for m in _OLD_FSTRING.finditer(text)]
    assert not found, found


def test_no_module_compiles_its_own_pattern_for_a_reference_to_state():
    """One pattern (`templating.STATE_REF`): a regex with a brace and `state\\.` anywhere else is a second one."""
    own = re.compile(r"""(?:r|rf|fr)?["'][^"'\n]*\\\{[^"'\n]*state\\\.[^"'\n]*["']""")
    found = [f"{p.relative_to(BACKEND)}: {m.group(0)}" for p in _sources() if p.name != "templating.py"
             for m in own.finditer(p.read_text())]
    assert not found, found


def test_the_editor_never_teaches_the_older_form():
    """The workflow editor's hints, placeholders and help: a quoted `{state.` is the older form (a JSX `{state.x}` is
    a variable, not a template)."""
    quoted = re.compile(r"""['"`]\{state\.""")
    src = REPO / "frontend" / "src"
    comment = re.compile(r"^\s*(?:\*|//|/\*)")  # a note may name the older form, saying it is converted
    found = [f"{p.relative_to(src)}:{n}" for p in [*src.rglob("*.ts"), *src.rglob("*.tsx")]
             for n, line in enumerate(p.read_text().splitlines(), 1) if quoted.search(line) and not comment.match(line)]
    assert not found, found
