"""A plan's Tool step never writes a whole object answer as text (F1402; CAP3's FF20 re-gate on 54f3ac82, prod-14 p1;
Naveen 11-05: "in v2.0", and the plan restated: a check at the plan check, beside F1394's, said once a run).

"Every Monday … summarise this week's launch notes … save it in the vault": the plan fetched the notes with
`scrape_url` and saved the summary with `vault_write` at `summaries/launch_notes_{{ state.fetch_launch_notes }}`. A
reference inside text is written as text, an object as its JSON (`steps.render_tool_arguments`), so the path was the
whole `{success, url, markdown}` answer and the run failed with Errno 36, a file name too long. The plan check now reads
each Tool step's arguments, and where a reference names an answer known to be an object (a built-in page read's, or a
tool's declared `outputSchema`) or a field of it that is a list or an object, and the argument is text (the reference
inside text, or alone where the tool declares that argument as text), the plan goes back once a run: put in the field
meant. A model's text answer, the run's own message and a field that is text all pass, as does an object handed on
whole where the argument takes one. The planner's tool list (`read_plan_context`) is as the re-gate ran it.
"""
from __future__ import annotations

import pytest

from core.native_builder import build_plan

MARK = "which would be written as its JSON"
FETCH = {"id": "fetch_launch_notes", "type": "tool", "name": "Fetch launch notes", "tool": "scrape_url",
         "does": "Reads this week's launch notes",
         "config": {"tool_arguments": {"url": "https://notes.example/launch"}}, "next": "summarize_notes"}
SUMMARIZE = {"id": "summarize_notes", "type": "llm", "name": "Summarise", "does": "Writes a three-line summary",
             "agent": "Writer", "prompt": "Summarise these notes in three lines: {{ state.fetch_launch_notes }}",
             "next": "save_summary"}


def _save(path: str, content: str = "{{ state.summarize_notes }}", tool: str = "vault_write") -> dict:
    return {"id": "save_summary", "type": "tool", "name": "Save summary to vault", "tool": tool,
            "does": "Writes the summary to the vault", "config": {"tool_arguments": {"path": path, "content": content}},
            "next": "end"}


# prod-14 p1's own step, as the re-gate saved it.
PROD14 = _save("summaries/launch_notes_{{ state.fetch_launch_notes }}")


def _plan(*steps: dict) -> dict:
    return {"kind": "create", "title": "Launch notes",
            "summary": "Every Monday, this week's launch notes in three lines.", "steps": list(steps), "agents": [{"name": "Writer", "purpose": "Writes short summaries."}]}


VAULT_TAKES = {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
               "required": ["path", "content"]}


def _ws(*tools: dict, takes: dict | None = None) -> build_plan.Workspace:
    return build_plan.Workspace(tools=[{"name": "scrape_url", "does": "Reads a web page"},
                                       {"name": "vault_write", "does": "Writes a file to the vault"}, *tools],
                                takes={"vault_write": VAULT_TAKES, **(takes or {})})


async def _said(plan: dict, ws: build_plan.Workspace | None = None, state: dict | None = None) -> list[str]:
    found = await build_plan.examine(plan, None, ws=ws or _ws(), state={"user_message": "x"} if state is None
                                     else state)
    return [e for e in found.errors if MARK in e]


class TestSentBack:
    async def test_prod14s_path_holds_the_whole_page_read(self):
        said = await _said(_plan(FETCH, SUMMARIZE, PROD14))
        assert len(said) == 1, said
        line = said[0]
        assert line.startswith("step save_summary: its argument `path` puts `{{ state.fetch_launch_notes }}` into "
                               "text, but that is the whole answer of step fetch_launch_notes, the object "
                               "`scrape_url` answers with `success`, `url`, `markdown`"), line
        # The way on: the field meant.
        assert "{{ state.fetch_launch_notes.markdown }}" in line, line

    async def test_any_text_argument_not_only_a_path(self):
        save = _save("summaries/launch.md", content="This week's notes: {{ state.fetch_launch_notes }}")
        said = await _said(_plan(FETCH, SUMMARIZE, save))
        assert len(said) == 1 and "its argument `content` puts `{{ state.fetch_launch_notes }}`" in said[0], said

    async def test_an_argument_inside_an_argument(self):
        send = {"id": "save_summary", "type": "tool", "name": "Post it", "tool": "post_message", "does": "Posts it",
                "config": {"tool_arguments": {"message": {"title": "Notes for {{ state.fetch_launch_notes }}"}}},
                "next": "end"}
        said = await _said(_plan(FETCH, SUMMARIZE, send), ws=_ws({"name": "post_message", "does": "Posts"}))
        assert len(said) == 1 and "its argument `message` puts" in said[0], said

    async def test_a_reference_alone_where_the_tool_takes_text(self):
        """Alone, a reference keeps its type: the tool is handed the object where it declared text."""
        said = await _said(_plan(FETCH, SUMMARIZE, _save("{{ state.fetch_launch_notes }}")))
        assert len(said) == 1 and "its argument `path` puts `{{ state.fetch_launch_notes }}`" in said[0], said

    async def test_a_list_field_of_the_answer(self):
        said = await _said(_plan(FETCH, SUMMARIZE, _save("summaries/{{ state.fetch_launch_notes.links }}.md")))
        assert len(said) == 1 and said[0].startswith(
            "step save_summary: its argument `path` puts `{{ state.fetch_launch_notes.links }}` into text, but that "
            "is a list in the answer of step fetch_launch_notes"), said

    async def test_a_declared_answer_and_its_object_field(self):
        tool = {"name": "notes_lookup", "does": "Gets the notes",
                "outputSchema": {"type": "object", "properties": {"title": {"type": "string"},
                                                                  "meta": {"type": "object"}}}}
        fetch = {**FETCH, "tool": "notes_lookup"}
        whole = await _said(_plan(fetch, SUMMARIZE, PROD14), ws=_ws(tool))
        assert len(whole) == 1 and "the object `notes_lookup` answers with `title`, `meta`" in whole[0], whole
        assert "{{ state.fetch_launch_notes.title }}" in whole[0], whole
        field = await _said(_plan(fetch, SUMMARIZE, _save("summaries/{{ state.fetch_launch_notes.meta }}.md")),
                            ws=_ws(tool))
        assert len(field) == 1 and "is an object in the answer of step fetch_launch_notes" in field[0], field

    async def test_said_once_a_run(self):
        state = {"user_message": "x"}
        assert len(await _said(_plan(FETCH, SUMMARIZE, PROD14), state=state)) == 1
        assert await _said(_plan(FETCH, SUMMARIZE, PROD14), state=state) == []

    async def test_said_once_beside_f1394s_line(self):
        """Each check keeps its own once: a plan with both faults hears both."""
        save = _save("summaries/{{ state.fetch_launch_notes.daily }}_{{ state.fetch_launch_notes }}.md")
        found = await build_plan.examine(_plan(FETCH, SUMMARIZE, save), None, ws=_ws(), state={"user_message": "x"})
        assert sum(MARK in e for e in found.errors) == 1, found.errors
        assert sum("an answer with no such field" in e for e in found.errors) == 1, found.errors


class TestLeftAlone:
    @pytest.mark.parametrize("path, content", [
        # prod-14 p2's own step: the run's message in the path, a model's text answer as the content.
        ("launch_summaries/summary_{{ state.user_input }}.txt", "{{ state.summarize_notes }}"),
        # A field of the answer that is text.
        ("summaries/launch.md", "{{ state.fetch_launch_notes.markdown }}"),
        ("summaries/{{ state.fetch_launch_notes.url }}.md", "{{ state.summarize_notes }}"),
        # A field deeper than the answer's own: not known, not held.
        ("summaries/{{ state.fetch_launch_notes.links.0 }}.md", "{{ state.summarize_notes }}"),
    ])
    async def test_text_where_text_is_meant(self, path, content):
        assert await _said(_plan(FETCH, SUMMARIZE, _save(path, content))) == []

    async def test_a_models_prompt_reads_the_whole_answer(self):
        """A model reads an object as its JSON: SUMMARIZE's prompt names the whole answer, and is not held."""
        assert await _said(_plan(FETCH, SUMMARIZE, _save("summaries/launch.md"))) == []

    @pytest.mark.parametrize("takes", [
        {"type": "object", "properties": {"path": {"type": "object"}}},
        {"type": "object", "properties": {"path": {}}},
        None,
    ])
    async def test_a_reference_alone_where_the_tool_takes_an_object_or_says_not(self, takes):
        save = _save("{{ state.fetch_launch_notes }}", tool="store_it")
        ws = _ws({"name": "store_it", "does": "Stores it"}, takes={"store_it": takes} if takes else None)
        assert await _said(_plan(FETCH, SUMMARIZE, save), ws=ws) == []

    async def test_an_answer_not_known(self):
        tool = {"name": "notes_lookup", "does": "Gets the notes"}
        assert await _said(_plan({**FETCH, "tool": "notes_lookup"}, SUMMARIZE, PROD14), ws=_ws(tool)) == []


async def test_the_workspace_reads_what_each_tool_takes_and_the_planners_list_is_as_it_was(monkeypatch):
    """Each tool's input schema is read for the check (an MCP tool's `inputSchema`, a function's `parameters`), and kept
    beside the tool list: the list the planner reads (`read_plan_context`'s pack) carries no more than it did."""
    from core import builder_tools, config
    from core.boards import config as boards_config
    from core.knowledge import config as kb_config
    from core.routes import agents, tools
    from core.store import resources

    class _Tool:
        def __init__(self, name, description, input_schema):
            self.name, self.description, self.inputSchema = name, description, input_schema

    async def catalogue(_server):
        return [_Tool("vault_write", "Writes a file to the vault.", VAULT_TAKES),
                {"function": {"name": "post_message", "description": "Posts a message.",
                              "parameters": {"type": "object", "properties": {"text": {"type": "string"}}}}},
                _Tool("no_schema", "Has none.", None)]

    async def nothing(*_a, **_k):
        return []

    monkeypatch.setattr(builder_tools, "_workspace_catalogue", catalogue)
    monkeypatch.setattr(agents, "load_user_agents", nothing)
    monkeypatch.setattr(resources, "load_orchestrations", nothing)
    monkeypatch.setattr(tools, "load_custom_tools", nothing)
    monkeypatch.setattr(kb_config, "knowledge_enabled", lambda: False)
    monkeypatch.setattr(boards_config, "boards_enabled", lambda: False)
    monkeypatch.setattr(config, "load_settings", lambda: {})
    ws = await build_plan.workspace(None)
    assert ws.takes == {"vault_write": VAULT_TAKES,
                        "post_message": {"type": "object", "properties": {"text": {"type": "string"}}}}
    assert ws.tools == [{"name": "vault_write", "does": "Writes a file to the vault."},
                        {"name": "post_message", "does": "Posts a message."},
                        {"name": "no_schema", "does": "Has none."}]


def test_the_answers_list_fields_are_fields_of_their_answers():
    from core.native_builder import structure

    for tool, fields in structure.ANSWER_LISTS.items():
        assert set(fields) <= set(structure.ANSWERS[tool]), tool


def test_its_said_flag_is_the_engines_own():
    from core.events_public import PRIVATE_STATE_PREFIXES

    assert build_plan._OBJECT_TEXT_SAID.startswith(PRIVATE_STATE_PREFIXES)
