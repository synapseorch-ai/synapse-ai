"""Every read of a Tool step's answer is checked to its depth against what its tool declares (F1620, part 1b).

A tool can say what it answers: a custom HTTP or Python tool's output schema, an MCP tool's `outputSchema`, or the
built-in page reads' own shape. A later step reading a field of a Tool step's answer that the tool does not answer
failed only when the run reached it (F1616(b)). Now the validator reads each tool's declaration into answer fields
(`step_answers.tool_answers`, through `output_fields.from_json_schema`) and checks every read to its end, as it checks
a model step's (F1618): in a template, a condition, a Loop's body through its item, and a Transform's code.

What it says depends on who owns the declaration:
- a custom tool's (the person's own, to edit) or a page read's (ours): an issue, with the fix, which is to read what it
  declares or add the field to the tool's output schema;
- an MCP tool's: a warning naming the path, since a server's own declaration can be wrong, and a build is never
  stopped by what the person cannot fix.

Near misses: a tool that declares nothing is never named; a read that holds is never named; the whole answer read is no
field; a page read is checked at its first level only, as its lists' items are not known.
"""
from __future__ import annotations

from core.builder_tools import _tool_answer_reads, _validate_orchestration
from core.orchestration import step_answers
from core.orchestration.output_fields import json_schema

LEADS = [{"name": "leads", "type": "list", "of": {"fields": [
    {"name": "email", "type": "string"}, {"name": "score", "type": "number", "may_be_empty": True}]}},
    {"name": "total", "type": "number"}]
CUSTOM = {"name": "find_leads", "does": "Finds leads", "outputSchema": json_schema(LEADS)}
MCP = {"name": "crm_leads", "does": "Lists leads", "outputSchema": json_schema(LEADS)}
LOOSE = {"name": "loose", "does": "Says anything"}


def answers(custom: bool = True) -> dict:
    return step_answers.tool_answers([CUSTOM, MCP, LOOSE], {"find_leads"} if custom else set())


def tool(sid: str, name: str = "find_leads") -> dict:
    return {"id": sid, "type": "tool", "forced_tool": name, "output_key": sid}


def show(sid: str, text: str) -> dict:
    return {"id": sid, "type": "print", "print_content": text, "output_key": f"o_{sid}"}


class TestTheAnswersAreRead:
    def test_each_declaring_tool_is_read_with_its_owner(self):
        got = answers()
        assert got["find_leads"]["owner"] == "custom" and got["crm_leads"]["owner"] == "mcp"
        assert [f["name"] for f in got["find_leads"]["fields"]] == ["leads", "total"]
        assert "loose" not in got, "a tool that declares nothing is not known"

    def test_the_page_reads_are_ours(self):
        page = answers()["scrape_url"]
        assert page["owner"] == "page" and "markdown" in {f["name"] for f in page["fields"]}


class TestEachReadToItsEnd:
    def test_a_misspelt_field_at_depth_is_named_with_its_fix(self):
        issues, warnings = _tool_answer_reads([tool("find"), show("p", "{{ state.find.leads.0.emial }}")], answers())
        assert len(issues) == 1 and "emial" in issues[0] and "`email`" in issues[0] and not warnings, issues
        assert "output schema" in issues[0], issues[0]

    def test_a_condition_a_loop_and_code_are_read(self):
        steps = [tool("find"),
                 {"id": "hot", "type": "if_else", "if_condition": "state.find.leads[0].scor > 0.5",
                  "if_true_step_id": "p", "if_false_step_id": "p"},
                 {"id": "each", "type": "loop", "loop_over": "state.find.leads", "loop_item_key": "lead",
                  "loop_step_ids": ["p"]},
                 show("p", "Call {{ state.lead.phone }}"),
                 {"id": "sum", "type": "transform", "transform_code": "result = state['find']['totl']",
                  "output_key": "sum"}]
        issues, _ = _tool_answer_reads(steps, answers())
        said = " ".join(issues)
        assert "scor" in said and "phone" in said and "totl" in said, issues

    def test_a_list_read_as_one_group_says_to_read_one_item(self):
        issues, _ = _tool_answer_reads([tool("find"), show("p", "{{ state.find.leads.email }}")], answers())
        assert issues and "list" in issues[0] and "leads.0.email" in issues[0], issues


class TestWhoOwnsTheDeclaration:
    def test_an_mcp_tools_read_past_its_declaration_is_a_warning(self):
        issues, warnings = _tool_answer_reads([tool("crm", "crm_leads"), show("p", "{{ state.crm.leads.0.emial }}")],
                                              answers())
        assert not issues and len(warnings) == 1, (issues, warnings)
        assert "declaration" in warnings[0] and "leads.0.emial" in warnings[0], warnings

    def test_the_same_read_on_a_custom_tool_is_an_issue(self):
        issues, warnings = _tool_answer_reads([tool("find"), show("p", "{{ state.find.leads.0.emial }}")], answers())
        assert issues and not warnings

    def test_a_page_read_is_checked_at_its_first_level_only(self):
        issues, _ = _tool_answer_reads([tool("page", "scrape_url"), show("p", "{{ state.page.daily }}")], answers())
        assert issues and "daily" in issues[0] and "markdown" in issues[0], issues
        deep, _ = _tool_answer_reads([tool("page", "scrape_url"), show("p", "{{ state.page.links.0.href }}")], answers())
        assert deep == [], "a page read's list items are not known"


class TestNearMisses:
    def test_a_tool_that_declares_nothing_is_never_named(self):
        assert _tool_answer_reads([tool("x", "loose"), show("p", "{{ state.x.anything.at.all }}")], answers()) == ([], [])

    def test_a_read_that_holds_and_the_whole_answer_are_never_named(self):
        steps = [tool("find"), show("p", "{{ state.find.leads.0.email }} of {{ state.find.total }}: {{ state.find }}")]
        assert _tool_answer_reads(steps, answers()) == ([], [])


class TestTheValidatorSaysThem:
    def orch(self, name: str) -> dict:
        return {"id": "o", "name": "Leads", "entry_step_id": "find", "steps": [
            {**tool("find", name), "name": "Find", "next_step_id": "p"},
            {**show("p", "{{ state.find.leads.0.emial }}"), "name": "Show", "next_step_id": None}]}

    def test_a_custom_tools_read_is_an_issue(self):
        verdict = _validate_orchestration(self.orch("find_leads"), tool_answers=answers())
        assert verdict["ok"] is False and any("emial" in i for i in verdict["issues"]), verdict

    def test_an_mcp_tools_read_keeps_the_workflow_ok_with_a_warning(self):
        verdict = _validate_orchestration(self.orch("crm_leads"), tool_answers=answers())
        assert not any("emial" in i for i in verdict["issues"]), verdict["issues"]
        assert any("emial" in w for w in verdict["warnings"]), verdict["warnings"]

    def test_without_the_tools_answers_nothing_is_said_of_them(self):
        verdict = _validate_orchestration(self.orch("find_leads"))
        assert not any("emial" in i for i in [*verdict["issues"], *verdict["warnings"]])
