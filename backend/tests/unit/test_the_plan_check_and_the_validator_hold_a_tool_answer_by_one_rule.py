"""The plan check and the validator hold every read of a Tool step's answer by one rule (F1620, part 1b-ii).

A plan's read of a field a Tool step's answer lacks was sent back once a run at the plan check (F1394), at its first
level only; the saved workflow's validator checks every read to its end against what the tool declares (F1620). Now the
plan check decides by the validator's rule, on the same reads and to the same depth: a template's, a condition's, a
Loop's body through its item, and a Transform's code. Each place says it in its own words: the plan check keeps F1394's
line for a first field the answer lacks, and its said-once; a read that goes wrong deeper in is said with what the
declaration has there.

Who owns the declaration decides what is said, in both places. A custom tool's (the person's own) or a page read's
(ours) sends the plan back, as before. An MCP tool's, which its server wrote and may have wrong, is said on the plan's
card and never sent back: the one difference from F1394, which sent every one back. The workspace tags each declared
answer with its owner as it lists the tools (`answer_owner`): `custom` for a custom tool's output schema, however its
catalogue entry carries it, else `mcp`. The validator tool reads the same tags: a custom or page read past the
declaration is an issue, an MCP one a warning.

A declaration with a part outside the answer's subset is known at its first level, the fields it names, as F1394 knew
it, in both places, and never deeper. For the validator that is a widening: such a custom tool's first field it lacks
is now an issue.

Near misses: a read that holds to its depth is never named; a read under a field such a declaration names is never
named; a tool that declares nothing is never tagged.
"""
from __future__ import annotations

import json
import types

import pytest

from core.builder_tools import _tool_answer_reads, _validate_orchestration, execute_builder_tool
from core.native_builder import build_plan
from core.orchestration import step_answers
from core.orchestration.output_fields import json_schema

LEADS = [{"name": "leads", "type": "list", "of": {"fields": [
    {"name": "email", "type": "string"}, {"name": "score", "type": "number", "may_be_empty": True}]}},
    {"name": "total", "type": "number"}]
SCHEMA = json_schema(LEADS)
#: Known at its first level only: a group with no fields is outside the answer's subset.
PARTLY = {"type": "object", "properties": {"hourly": {"type": "object"}, "daily": {"type": "string"}}}


def _ws_tool(name: str, owner: str | None, schema: dict = SCHEMA) -> dict:
    return {"name": name, "does": "Finds leads", "outputSchema": schema, **({"answer_owner": owner} if owner else {})}


WS = build_plan.Workspace(tools=[{"name": "scrape_url", "does": "Reads a web page"},
                                 _ws_tool("find_leads", "custom"), _ws_tool("crm_leads", "mcp")])


def _find(tool: str = "find_leads", nxt: str = "show") -> dict:
    return {"id": "find", "type": "tool", "name": "Find leads", "does": "Finds this week's leads", "tool": tool,
            "next": nxt}


def _show(text: str, sid: str = "show", nxt: str | None = "end") -> dict:
    return {"id": sid, "type": "print", "name": "Show the lead", "does": "Shows the lead", "prompt": text,
            **({"next": nxt} if nxt else {})}


def _plan(*steps: dict) -> dict:
    return {"kind": "create", "title": "Leads", "summary": "Shows this week's leads.", "steps": list(steps)}


async def _examined(plan: dict, ws: build_plan.Workspace = WS, state: dict | None = None):
    return await build_plan.examine(plan, None, ws=ws, state={"user_message": "x"} if state is None else state)


def _of(lines: list[str], word: str) -> list[str]:
    return [line for line in lines if word in line]


class TestTheOwnerIsTagged:
    @pytest.fixture
    def listed(self, monkeypatch):
        """The catalogue as `_workspace_catalogue` gives it: an MCP tool with its `outputSchema`; a custom tool whose
        entry carries its schema too; a custom tool whose entry carries none, its schema stored; a tool with none."""
        def tool(name: str, schema: dict | None = None):
            return types.SimpleNamespace(name=name, description=f"{name}.", inputSchema={"type": "object"},
                                         outputSchema=schema)

        async def catalogue(server_module):
            return [tool("crm_leads", SCHEMA), tool("find_leads", SCHEMA), tool("stored_leads"), tool("loose")]

        async def custom():
            return [{"name": "find_leads", "outputSchema": SCHEMA}, {"name": "stored_leads", "outputSchema": SCHEMA},
                    {"name": "plain_custom"}]

        async def named(server_module):
            return frozenset()

        monkeypatch.setattr("core.builder_tools._workspace_catalogue", catalogue)
        monkeypatch.setattr("core.routes.tools.load_custom_tools", custom)
        monkeypatch.setattr("core.tools.named_only_offered", named)

    async def test_a_custom_tools_declared_answer_is_its_own_never_mcp(self, listed):
        ws = await build_plan.workspace(None)
        owners = {t["name"]: t.get("answer_owner") for t in ws.tools}
        assert owners["find_leads"] == "custom", "a custom tool whose catalogue entry carries its schema is still custom"
        assert owners["stored_leads"] == "custom"
        assert owners["crm_leads"] == "mcp"

    async def test_a_tool_that_declares_nothing_is_never_tagged(self, listed):
        ws = await build_plan.workspace(None)
        assert "answer_owner" not in next(t for t in ws.tools if t["name"] == "loose")


class TestThePlanCheckDecidesByTheValidatorsRule:
    async def test_a_read_past_a_custom_tools_answer_at_depth_is_sent_back_with_what_it_has(self):
        found = await _examined(_plan(_find(), _show("{{ state.find.leads.0.emial }}")))
        said = _of(found.errors, "emial")
        assert len(said) == 1, found.errors
        assert said[0].startswith("step show: its template reads `leads.0.emial` from `state.find`"), said[0]
        assert "`email`" in said[0] and "find_leads" in said[0], said[0]

    async def test_a_first_field_it_lacks_keeps_f1394s_line(self):
        found = await _examined(_plan(_find(), _show("{{ state.find.leadz }}")))
        said = _of(found.errors, "an answer with no such field")
        assert len(said) == 1, found.errors
        assert said[0].startswith("step show: its template reads `leadz` from `state.find`, an answer with no such "
                                  "field: step find calls `find_leads`, which answers an object with `leads`, "
                                  "`total`"), said[0]

    async def test_a_condition_is_read_to_its_depth(self):
        cond = {"id": "hot", "type": "if_else", "name": "Hot lead?", "does": "Checks the first lead's score",
                "condition": "state.find.leads[0].scor > 0.5", "branches": {"true": "show", "false": "show"}}
        found = await _examined(_plan(_find(nxt="hot"), cond, _show("{{ state.find.total }}")))
        said = _of(found.errors, "scor")
        assert len(said) == 1 and said[0].startswith("step hot: its condition reads `leads.0.scor`"), found.errors

    async def test_a_loops_body_is_read_through_its_item(self):
        loop = {"id": "each", "type": "loop", "name": "Each lead", "does": "Goes through the leads",
                "loop_over": "state.find.leads", "body": ["call"], "config": {"loop_item_key": "lead"}, "next": "end"}
        found = await _examined(_plan(_find(nxt="each"), loop, _show("Call {{ state.lead.phone }}", "call", None)))
        said = _of(found.errors, "phone")
        assert len(said) == 1 and said[0].startswith("step call: its template reads `leads.0.phone`"), found.errors

    async def test_said_once_a_run(self):
        state = {"user_message": "x"}
        plan = _plan(_find(), _show("{{ state.find.leads.0.emial }}"))
        assert len(_of((await _examined(plan, state=state)).errors, "emial")) == 1
        assert _of((await _examined(plan, state=state)).errors, "emial") == []

    async def test_a_read_that_holds_to_its_depth_is_never_named(self):
        found = await _examined(_plan(_find(), _show("{{ state.find.leads.0.email }} of {{ state.find.total }}: "
                                                     "{{ state.find }}")))
        assert not _of(found.errors, "state.find"), found.errors


class TestAnMcpToolsReadIsSaidOnTheCard:
    """The one difference from F1394: an MCP server's own declaration may be wrong, so the plan is never sent back
    for it; the card says it."""

    @pytest.mark.parametrize("read, path", [("leads.0.emial", "leads.0.emial"), ("leadz", "leadz")],
                             ids=["at depth", "its first field"])
    async def test_never_sent_back_and_said_on_the_card(self, read, path):
        found = await _examined(_plan(_find("crm_leads"), _show(f"{{{{ state.find.{read} }}}}")))
        assert not _of(found.errors, path.split(".")[-1]), found.errors
        said = _of(found.notes, path)
        assert len(said) == 1 and "crm_leads" in said[0], found.notes

    async def test_an_untagged_declaration_is_its_servers(self):
        ws = build_plan.Workspace(tools=[_ws_tool("crm_leads", None)])
        found = await _examined(_plan(_find("crm_leads"), _show("{{ state.find.leads.0.emial }}")), ws=ws)
        assert not _of(found.errors, "emial") and _of(found.notes, "emial"), (found.errors, found.notes)


class TestADeclarationPartlyOutsideTheSubset:
    def test_it_is_known_at_its_first_level(self):
        answer = step_answers.tool_answers([{"name": "forecast", "outputSchema": PARTLY}], {"forecast"})["forecast"]
        assert answer["owner"] == "custom" and answer.get("first_level") is True, answer
        assert [f["name"] for f in answer["fields"]] == ["hourly", "daily"], answer

    def test_the_validator_names_a_first_field_it_lacks(self):
        answers = step_answers.tool_answers([{"name": "forecast", "outputSchema": PARTLY}], {"forecast"})
        steps = [{"id": "f", "type": "tool", "forced_tool": "forecast", "output_key": "f"},
                 {"id": "p", "type": "print", "print_content": "{{ state.f.weekly }}", "output_key": "o"}]
        issues, _ = _tool_answer_reads(steps, answers)
        assert len(issues) == 1 and "weekly" in issues[0], issues

    def test_a_read_under_a_field_it_names_is_never_named(self):
        answers = step_answers.tool_answers([{"name": "forecast", "outputSchema": PARTLY}], {"forecast"})
        steps = [{"id": "f", "type": "tool", "forced_tool": "forecast", "output_key": "f"},
                 {"id": "p", "type": "print", "print_content": "{{ state.f.hourly.temp }} {{ state.f.daily }}",
                  "output_key": "o"}]
        assert _tool_answer_reads(steps, answers) == ([], [])


class TestOneReadsGatherer:
    def test_a_conditions_get_is_a_read_in_the_validator_too(self):
        """F1394's reader catches `state.x.get('f')` in a condition; the validator's deep reads drop the method."""
        steps = [{"id": "find", "type": "tool", "forced_tool": "find_leads", "output_key": "find"},
                 {"id": "hot", "type": "if_else", "if_condition": "state.find.get('totl') > 3",
                  "if_true_step_id": None, "if_false_step_id": None}]
        issues, _ = _tool_answer_reads(steps, step_answers.tool_answers([{"name": "find_leads",
                                                                          "outputSchema": SCHEMA}], {"find_leads"}))
        assert len(issues) == 1 and "totl" in issues[0], issues

    def test_the_same_read_twice_in_one_step_is_said_once(self):
        steps = [{"id": "crm", "type": "tool", "forced_tool": "crm_leads", "output_key": "crm"},
                 {"id": "hot", "type": "if_else", "if_condition": "state.crm.totl > 3", "if_true_step_id": None,
                  "if_false_step_id": None, "prompt_template": "{{ state.crm.totl }}"}]
        _, warnings = _tool_answer_reads(steps, step_answers.tool_answers([{"name": "crm_leads",
                                                                            "outputSchema": SCHEMA}]))
        assert len(warnings) == 1, warnings


class TestTheValidatorToolReadsTheTags:
    @pytest.fixture
    def listed(self, monkeypatch):
        def tool(name: str, schema: dict | None = None):
            return types.SimpleNamespace(name=name, description=f"{name}.", inputSchema={"type": "object"},
                                         outputSchema=schema)

        async def catalogue(server_module):
            return [tool("crm_leads", SCHEMA), tool("find_leads")]

        async def custom():
            return [{"name": "find_leads", "description": "Finds leads", "tool_type": "http", "method": "GET",
                     "url": "https://example.com/leads", "inputSchema": {"type": "object"}, "outputSchema": SCHEMA}]

        monkeypatch.setattr("core.builder_tools._workspace_catalogue", catalogue)
        monkeypatch.setattr("core.routes.tools.load_custom_tools", custom)

    @staticmethod
    async def _verdict(tool: str) -> dict:
        server = types.SimpleNamespace(agent_sessions={})
        steps = [{"id": "find", "name": "Find", "type": "tool", "forced_tool": tool, "output_key": "find",
                  "next_step_id": "show"},
                 {"id": "show", "name": "Show", "type": "print", "print_content": "{{ state.find.leads.0.emial }}",
                  "output_key": "shown", "next_step_id": None}]
        made = json.loads(await execute_builder_tool("create_orchestration", {
            "name": "Leads", "description": "Shows the leads", "entry_step_id": "find",
            "steps_json": json.dumps(steps)}, server))
        assert "error" not in made, made
        return json.loads(await execute_builder_tool("validate_orchestration",
                                                     {"orch_id": made["orchestration"]["id"]}, server))

    async def test_a_custom_tools_read_past_its_answer_is_an_issue(self, listed):
        verdict = await self._verdict("find_leads")
        assert _of(verdict.get("issues", []), "emial"), verdict

    async def test_an_mcp_tools_is_a_warning(self, listed):
        verdict = await self._verdict("crm_leads")
        assert not _of(verdict.get("issues", []), "emial"), verdict
        assert _of(verdict.get("warnings", []), "emial"), verdict


def test_the_saved_validator_with_its_answers_and_the_plan_check_agree_on_a_custom_tool():
    """The same read, the same verdict: the validator's issue and the plan check's line name the same path."""
    orch = {"id": "o", "name": "Leads", "entry_step_id": "find", "steps": [
        {"id": "find", "name": "Find", "type": "tool", "forced_tool": "find_leads", "output_key": "find",
         "next_step_id": "show"},
        {"id": "show", "name": "Show", "type": "print", "print_content": "{{ state.find.leads.0.emial }}",
         "output_key": "shown", "next_step_id": None}]}
    verdict = _validate_orchestration(orch, tool_answers=step_answers.tool_answers(
        [{"name": "find_leads", "outputSchema": SCHEMA}], {"find_leads"}))
    assert _of(verdict["issues"], "leads.0.emial"), verdict
