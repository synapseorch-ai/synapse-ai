"""A Tool step's set arguments fit its tool's schema all the way down, before a run finds them wrong (F1357, cap-01;
eb 11-04).

CAP3's FF18 gate, cap-01: the builder set `scrape_with_session`'s arguments on the step, and its second browser step
had no `url`. The tool's schema needs one in every item of `steps`, so every run failed ("Input validation error:
'url' is a required property"). The plan check and `validate_orchestration` both passed it: `argument_issues` (E1)
read only the schema's top level, a name it does not take or a required one left out.

Now the whole arguments are held to the schema with jsonschema, for every tool a step can name (one lookup, the run's),
and each misfit is said with where it is: a required name left out, a value of the wrong type, a name an object that
sets `additionalProperties` does not take. Only what the schema declares: no `format`, and a `$ref` resolved within the
schema, never over the network. A value that is or holds a state reference is state's to fill, so it is never held to
a type. A tool whose schema cannot be read now is skipped, never refused. The person's editor save is not this check's
door: a workflow they save there is saved as before.
"""
from __future__ import annotations

import copy
import json
import types

import pytest

from core.tools import VirtualTool

#: `scrape_with_session`'s schema, as tools/web_scraper.py declares it (its descriptions left out).
SCRAPE = {
    "type": "object",
    "properties": {
        "session_id": {"type": "string"},
        "steps": {"type": "array", "minItems": 1, "items": {
            "type": "object",
            "properties": {"url": {"type": "string"}, "js_code": {"type": "string"}, "wait_for": {"type": "string"},
                           "extract": {"type": "boolean", "default": True}},
            "required": ["url"]}},
        "headers": {"type": "object", "additionalProperties": {"type": "string"}},
    },
    "required": ["session_id", "steps"],
}

#: cap-01's step, as the builder set it (ff18-c1-cli-claude-claude-haiku-4-5 records).
CAP01 = {"session_id": "portal.localhost",
         "steps": [{"tool": "browser_navigate", "url": "http://portal.localhost:9411"},
                   {"tool": "browser_snapshot"}]}


def _orch(arguments, *, tool: str = "scrape_with_session", orch_id: str = "orch_f1357") -> dict:
    return {
        "id": orch_id, "name": "Supplier portal", "entry_step_id": "fetch", "state_schema": {},
        "steps": [
            {"id": "fetch", "name": "Fetch supplier portal", "type": "tool", "forced_tool": tool,
             "tool_arguments": arguments, "output_key": "portal", "next_step_id": "end"},
            {"id": "end", "name": "End", "type": "end"},
        ],
    }


@pytest.fixture
def declared():
    """The tools a lookup finds, by name; a test adds its own."""
    return {"scrape_with_session": VirtualTool("scrape_with_session", "scrapes", copy.deepcopy(SCRAPE))}


@pytest.fixture
def server(monkeypatch, declared):
    import core.routes.tools as routes_tools
    import core.tools as tools_mod

    async def one(server_module, name, agent, custom_tools):
        found = declared.get(name)
        if isinstance(found, Exception):
            raise found
        return found

    async def no_custom():
        return []

    monkeypatch.setattr(tools_mod, "resolve_one_tool", one)
    monkeypatch.setattr(routes_tools, "load_custom_tools", no_custom)
    return types.SimpleNamespace(agent_sessions={}, tool_router={})


async def _issues(arguments, server, **kw) -> list[str]:
    from core import builder_guard

    return await builder_guard.argument_issues([_orch(arguments, **kw)], server)


class TestCap01:
    async def test_a_browser_step_without_its_url_is_said_with_where_it_is(self, server):
        issues = await _issues(CAP01, server)
        assert len(issues) == 1, issues
        said = issues[0]
        assert "'Fetch supplier portal'" in said and "scrape_with_session" in said, said
        assert "steps[1]" in said and "url" in said, said
        assert "steps[0]" not in said, "the first step has its url"

    async def test_with_every_url_set_it_fits(self, server):
        fixed = {**CAP01, "steps": [{**s, "url": "http://portal.localhost:9411"} for s in CAP01["steps"]]}
        assert await _issues(fixed, server) == []

    async def test_validate_orchestration_refuses_it(self, server, client):
        from core.builder_tools import execute_builder_tool_with_events
        from core.store.resources import save_orchestration

        await save_orchestration(_orch(CAP01))
        raw, _ = await execute_builder_tool_with_events("validate_orchestration", {"orch_id": "orch_f1357"}, server)
        verdict = json.loads(raw)
        assert verdict["ok"] is False, verdict
        assert any("steps[1]" in i and "url" in i for i in verdict["issues"]), verdict["issues"]


class TestWhatTheSchemaDeclares:
    async def test_a_top_level_name_left_out_is_still_said_once(self, server):
        issues = await _issues({"steps": [{"url": "https://a.example"}]}, server)
        assert len(issues) == 1 and "'session_id'" in issues[0], issues

    async def test_a_nested_value_of_the_wrong_type_is_said(self, server):
        issues = await _issues({**CAP01, "steps": [{"url": "https://a.example", "extract": "yes"}]}, server)
        assert len(issues) == 1, issues
        assert "steps[0].extract" in issues[0] and "boolean" in issues[0], issues

    async def test_a_value_where_additional_properties_names_a_type_is_held_to_it(self, server):
        issues = await _issues({**CAP01, "steps": [{"url": "https://a.example"}], "headers": {"X-Page": 5}}, server)
        assert len(issues) == 1 and "headers.X-Page" in issues[0] and "string" in issues[0], issues

    async def test_a_name_an_object_does_not_take_is_said_where_it_says_so(self, server, declared):
        schema = {"type": "object", "properties": {"row": {"type": "object", "properties": {"id": {"type": "string"}},
                                                         "additionalProperties": False}}}
        declared["put_row"] = VirtualTool("put_row", "puts", schema)
        issues = await _issues({"row": {"id": "1", "colour": "red"}}, server, tool="put_row")
        assert len(issues) == 1 and "row" in issues[0] and "colour" in issues[0], issues

    async def test_an_extra_name_where_the_schema_allows_one_is_fine(self, server):
        assert await _issues({**CAP01, "steps": [{"url": "https://a.example", "note": "x"}]}, server) == []

    async def test_a_format_is_never_checked(self, server, declared):
        declared["mail"] = VirtualTool("mail", "mails", {"type": "object", "properties": {
            "to": {"type": "string", "format": "email"}}})
        assert await _issues({"to": "not an address"}, server, tool="mail") == []

    async def test_a_ref_within_the_schema_is_followed(self, server, declared):
        declared["count"] = VirtualTool("count", "counts", {"type": "object", "$defs": {"n": {"type": "integer"}},
                                                           "properties": {"n": {"$ref": "#/$defs/n"}}})
        issues = await _issues({"n": "three"}, server, tool="count")
        assert len(issues) == 1 and "n" in issues[0] and "integer" in issues[0], issues

    async def test_a_ref_elsewhere_is_never_fetched_and_never_refused(self, server, declared, monkeypatch):
        import urllib.request

        def no_network(*a, **k):
            raise AssertionError("a schema's $ref was fetched over the network")

        monkeypatch.setattr(urllib.request, "urlopen", no_network)
        declared["far"] = VirtualTool("far", "far", {"type": "object", "properties": {
            "n": {"$ref": "https://schemas.example/n.json"}}})
        assert await _issues({"n": "three"}, server, tool="far") == []


class TestStatesToFill:
    @pytest.mark.parametrize("steps", ["{{state.portal_steps}}", "{{ state.portal_steps }}", "{state.portal_steps}"])
    async def test_a_reference_standing_for_a_value_is_never_held_to_its_type(self, server, steps):
        assert await _issues({"session_id": "s", "steps": steps}, server) == []

    async def test_a_reference_inside_an_item_is_never_held_to_its_type(self, server):
        assert await _issues({**CAP01, "steps": [{"url": "{{state.portal}}", "extract": "{{state.whole}}"}]},
                             server) == []

    async def test_a_reference_never_stands_in_for_a_name_left_out(self, server):
        issues = await _issues({**CAP01, "steps": [{"url": "{{state.portal}}"}, {"js_code": "{{state.login}}"}]},
                               server)
        assert len(issues) == 1 and "steps[1]" in issues[0] and "url" in issues[0], issues


class TestEveryKindOfTool:
    @pytest.mark.parametrize("kind", ["http", "python"])
    async def test_a_custom_tools_schema_is_held_the_same(self, monkeypatch, kind):
        """The run's own lookup, not a stand-in: an HTTP or Python tool's schema is the one it was saved with."""
        import core.routes.tools as routes_tools
        from core import builder_guard

        async def custom():
            return [{"name": "post_rows", "type": kind, "description": "posts", "inputSchema": {
                "type": "object", "properties": {"rows": {"type": "array", "items": {
                    "type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}}}}}]

        monkeypatch.setattr(routes_tools, "load_custom_tools", custom)
        server = types.SimpleNamespace(agent_sessions={}, tool_router={})
        issues = await builder_guard.argument_issues([_orch({"rows": [{"id": "1"}, {}]}, tool="post_rows")], server)
        assert len(issues) == 1 and "rows[1]" in issues[0] and "id" in issues[0], issues

    async def test_a_tool_the_build_makes_is_held_to_what_it_will_take(self, server):
        from core import builder_guard

        made = {"post_rows": {"type": "object", "properties": {"rows": {"type": "array", "items": {
            "type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}}}}}
        issues = await builder_guard.argument_issues([_orch({"rows": [{}]}, tool="post_rows")], server, made)
        assert len(issues) == 1 and "rows[0]" in issues[0], issues


class TestNeverRefusedForWhatCannotBeRead:
    async def test_a_tool_whose_schema_cannot_be_read_now_is_skipped(self, server, declared):
        declared["scrape_with_session"] = ConnectionError("the scraper's server is down")
        assert await _issues(CAP01, server) == []

    async def test_a_tool_nothing_answers_for_now_is_left_to_f489(self, server, declared):
        del declared["scrape_with_session"]
        assert await _issues(CAP01, server) == []

    async def test_a_schema_that_is_not_one_is_skipped(self, server, declared):
        declared["odd"] = VirtualTool("odd", "odd", {"type": "object", "properties": {"n": {"type": 7}}})
        assert await _issues({"n": 1}, server, tool="odd") == []

    async def test_arguments_the_model_writes_are_not_checked(self, server):
        assert await _issues(None, server) == []


async def test_the_persons_editor_save_keeps_a_workflow_this_check_would_say(server, client):
    """The editor's save is the person's: it refuses only what the engine would not run (F850), never this."""
    from core.store.resources import get_orchestration

    resp = await client.post("/api/orchestrations", json=_orch(CAP01, orch_id="orch_f1357_editor"))
    assert resp.status_code == 200, resp.text
    kept = await get_orchestration("orch_f1357_editor")
    assert kept and kept["steps"][0]["tool_arguments"] == CAP01
