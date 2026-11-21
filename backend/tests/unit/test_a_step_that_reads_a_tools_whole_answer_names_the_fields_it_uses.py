"""A step that reads a tool's whole answer names the fields of it that it uses, and a field the tool's declaration
lacks goes back to the planner (F1675).

Asked for the humidity, a plan read a weather tool's whole answer in an `llm` step and said "What I assumed: the tool
returns humidity data", though the tool declares its answer as `{city, temp_c}`; the run said, honestly, that there
was no humidity. The plan check holds a field read (`state.find.total`) to the tool's declaration, but a whole answer
read in a prompt (`{{ state.find }}`) is no field read, so nothing held the plan to what the tool answers.

Now such a step names what it uses in `uses_fields` (`find.total`), and each is held by the same rule as a field read:
a field a custom tool's declaration lacks goes back with what it has; one an MCP tool's lacks is said on the card,
since its server's word may be wrong. The planner is told to read the declaration before assuming a field.

Near misses: a field the declaration has is silent; a step that reads the whole answer without `uses_fields` is not
flagged; an MCP tool's missing field is never sent back.
"""
from __future__ import annotations

from pathlib import Path

from core.native_builder import build_plan
from core.orchestration.output_fields import json_schema

FIELDS = [{"name": "leads", "type": "list", "of": {"fields": [{"name": "email", "type": "string"}]}},
          {"name": "total", "type": "number"}]
SCHEMA = json_schema(FIELDS)
WS = build_plan.Workspace(tools=[
    {"name": "find_leads", "does": "Finds leads", "outputSchema": SCHEMA, "answer_owner": "custom"},
    {"name": "crm_leads", "does": "Finds leads", "outputSchema": SCHEMA, "answer_owner": "mcp"}])
PLAN_STEPS = Path(build_plan.__file__).resolve().parent / "prompts" / "plan_steps.md"


def _plan(uses: list[str] | None, tool: str = "find_leads") -> dict:
    say = {"id": "say", "type": "llm", "name": "Say it", "does": "Reports this week's leads",
           "prompt": "Report this week's leads and how warm they are: {{ state.find }}", "next": "end"}
    if uses is not None:
        say["uses_fields"] = uses
    return {"kind": "create", "title": "Leads", "summary": "Reports this week's leads.",
            "steps": [{"id": "find", "type": "tool", "name": "Find leads", "does": "Finds this week's leads",
                       "tool": tool, "next": "say"}, say]}


async def _examined(plan: dict):
    return await build_plan.examine(plan, None, ws=WS, state={"user_message": "x"})


def _of(lines: list[str], word: str) -> list[str]:
    return [line for line in lines if word in line]


class TestAFieldTheDeclarationLacks:
    async def test_a_custom_tool_s_goes_back_with_what_it_has(self):
        found = await _examined(_plan(["find.warmth"]))
        [said] = _of(found.errors, "warmth")
        assert said.startswith("step say: uses `warmth` from `state.find`"), said
        assert "`find_leads`" in said and "`leads`, `total`" in said, said
        assert "find a tool that answers it" in said, said

    async def test_an_mcp_tool_s_is_said_on_the_card_never_sent_back(self):
        found = await _examined(_plan(["find.warmth"], tool="crm_leads"))
        assert not _of(found.errors, "warmth"), found.errors
        assert _of(found.notes, "warmth"), found.notes


class TestNearMisses:
    async def test_a_field_the_declaration_has_is_silent(self):
        found = await _examined(_plan(["find.total", "find.leads"]))
        assert not _of(found.errors, "uses `") and not _of(found.notes, "uses `"), (found.errors, found.notes)

    async def test_a_whole_answer_read_without_uses_fields_is_not_flagged(self):
        found = await _examined(_plan(None))
        assert not _of(found.errors, "state.find"), found.errors

    async def test_the_step_keeps_what_it_uses(self):
        found = await _examined(_plan(["find.total"]))
        assert found.plan is not None
        assert next(s for s in found.plan.steps if s.id == "say").uses_fields == ["find.total"]


class TestThePlanner:
    def test_it_is_told_to_read_the_declaration_and_name_what_it_uses(self):
        text = PLAN_STEPS.read_text(encoding="utf-8")
        assert "`uses_fields`" in text and "never assume a field" in text
