"""A Loop never goes through a step's whole answer object (F1613).

A model step that declares its answer's fields (F1553) answers one object, such as `{"keywords": [...]}`. A Loop
whose list is that step, `loop_over: state.keyword_researcher`, went through the object: the engine warned "dict,
not a list — zero iterations", the run said Completed, and the step after the loop summarised research that never
ran. Nothing at build or validate time noticed. Now:

* building a plan, a Loop over a step that answers one list field goes through that field, and the card says so;
  a step with no list field, or with several, is sent back to the planner naming each field and its type, and so
  is a declared field that is not a list;
* validating a workflow (saved by hand, imported, or written by the tools), the same reads are refused with the fix
  named, wherever every step writing the key says its answer's fields;
* what may hold a list is left alone: a field that is a list, a Tool's or a Transform's answer (they say no
  fields), a model's text declared a list in the state schema (F14).
"""
from __future__ import annotations

from core.builder_tools import _validate_orchestration
from core.native_builder import build_plan
from core.native_builder import objects as O
from core.native_builder.compile import compile_plan

KEYWORDS = {"name": "keywords", "type": "list", "description": "The keywords"}
NOTES = {"name": "notes", "type": "string", "description": "What it noticed"}
SOURCES = {"name": "sources", "type": "list", "description": "Where it looked"}


def _researcher(*fields: dict) -> dict:
    return {"id": "keyword_researcher", "type": "llm", "name": "Keyword researcher", "does": "Finds keywords",
            "prompt": "Find three keywords for {{ state.user_input }}.",
            "config": {"output_fields": list(fields)}, "next": "each"}


def _loop(over: str) -> dict:
    return {"id": "each", "type": "loop", "name": "Research loop", "does": "Researches each keyword",
            "loop_over": over, "body": ["research"], "next": "summary"}


RESEARCH = {"id": "research", "type": "llm", "name": "Research", "does": "Researches one keyword",
            "prompt": "Research the keyword {{ state.loop_item }}."}
SUMMARY = {"id": "summary", "type": "llm", "name": "Summary", "does": "Sums up the research",
           "prompt": "Sum up this research: {{ state.research }}", "next": "end"}


def _plan(*fields: dict, over: str = "keyword_researcher") -> dict:
    return {"kind": "create", "title": "Keyword research", "summary": "Researches keywords.",
            "steps": [_researcher(*fields), _loop(over), RESEARCH, SUMMARY]}


async def _examined(plan: dict):
    return await build_plan.examine(plan, None, ws=build_plan.Workspace(), state={"user_message": "x"})


def _loop_said(lines: list[str]) -> list[str]:
    return [line for line in lines if "each" in line or "Research loop" in line]


def _mint():
    n = {"i": 0}

    def mint(prefix, length=7):
        n["i"] += 1
        return f"{prefix}_{n['i']:03d}"

    return mint


class TestBuildingAPlan:
    async def test_a_loop_over_a_step_with_one_list_field_goes_through_that_field(self):
        found = await _examined(_plan(KEYWORDS, NOTES))
        assert not _loop_said(found.errors), found.errors
        loop = next(s for s in found.plan.steps if s.id == "each")
        assert loop.loop_over.removeprefix("state.") == "keyword_researcher.keywords", loop.loop_over

    async def test_the_card_says_which_field_the_loop_goes_through(self):
        found = await _examined(_plan(KEYWORDS, NOTES))
        said = [n for n in found.notes if "keywords" in n]
        assert len(said) == 1, found.notes
        assert "Research loop" in said[0] and "Keyword researcher" in said[0], said[0]

    async def test_a_step_with_two_list_fields_is_sent_back_naming_them(self):
        found = await _examined(_plan(KEYWORDS, SOURCES, NOTES))
        said = _loop_said(found.errors)
        assert len(said) == 1, found.errors
        for words in ("keyword_researcher", "keywords", "sources", "list"):
            assert words in said[0], (words, said[0])

    async def test_a_step_with_no_list_field_is_sent_back_naming_its_fields(self):
        found = await _examined(_plan(NOTES))
        said = _loop_said(found.errors)
        assert len(said) == 1, found.errors
        assert "notes" in said[0] and ("no list" in said[0].lower() or "not a list" in said[0]), said[0]

    async def test_a_declared_field_that_is_not_a_list_is_sent_back(self):
        found = await _examined(_plan(KEYWORDS, NOTES, over="keyword_researcher.notes"))
        said = _loop_said(found.errors)
        assert len(said) == 1, found.errors
        assert "notes" in said[0] and "string" in said[0] and "keywords" in said[0], said[0]

    async def test_a_loop_over_a_list_field_is_built_as_it_is(self):
        found = await _examined(_plan(KEYWORDS, NOTES, over="keyword_researcher.keywords"))
        assert not _loop_said(found.errors), found.errors
        assert not [n for n in found.notes if "Research loop" in n], found.notes

    def test_compile_alone_never_saves_a_loop_over_the_whole_object(self):
        """compile_plan is also called without examine (an edit's recompile): it states the issue, never saves it."""
        plan = O.Plan.from_draft(_plan(KEYWORDS, SOURCES))
        compiled = compile_plan(plan, mint=_mint())
        assert _loop_said(compiled.issues), compiled.issues


# ── validating a workflow as saved ────────────────────────────────────────────────────────────────────────────────

def _llm(fields: list[dict] | None, key: str = "keyword_researcher") -> dict:
    step = {"id": key, "name": "Keyword researcher", "type": "llm", "model": "gpt-4o",
            "prompt_template": "Find keywords for {{ state.user_input }}.", "output_key": key, "next_step_id": "each"}
    if fields is not None:
        step["output_fields"] = fields
    return step


def _engine_loop(over: str) -> dict:
    return {"id": "each", "name": "Research loop", "type": "loop", "loop_over": over, "loop_step_ids": ["research"],
            "next_step_id": "summary"}


ENGINE_BODY = {"id": "research", "name": "Research", "type": "llm", "model": "gpt-4o",
               "prompt_template": "Research {{ state.loop_item }}.", "output_key": "research"}
ENGINE_SUMMARY = {"id": "summary", "name": "Summary", "type": "llm", "model": "gpt-4o",
                  "prompt_template": "Sum up: {{ state.research }}", "output_key": "summary"}


def _workflow(first: dict, over: str, **extra) -> dict:
    return {"id": "orch_f1613", "name": "Keyword research", "entry_step_id": first["id"],
            "steps": [first, _engine_loop(over), ENGINE_BODY, ENGINE_SUMMARY], **extra}


def _issues(orch: dict) -> list[str]:
    return [i for i in _validate_orchestration(orch)["issues"] if "Loop 'each'" in i or "loop_over" in i]


class TestValidatingAWorkflow:
    def test_the_whole_answer_of_a_step_with_one_list_field_is_refused_naming_the_field(self):
        said = _issues(_workflow(_llm([KEYWORDS, NOTES]), "state.keyword_researcher"))
        assert len(said) == 1, said
        assert "state.keyword_researcher.keywords" in said[0], said[0]

    def test_the_whole_answer_of_a_step_with_two_list_fields_is_refused_naming_both(self):
        said = _issues(_workflow(_llm([KEYWORDS, SOURCES]), "state.keyword_researcher"))
        assert len(said) == 1, said
        assert "keywords" in said[0] and "sources" in said[0], said[0]

    def test_a_declared_field_that_is_not_a_list_is_refused(self):
        said = _issues(_workflow(_llm([KEYWORDS, NOTES]), "state.keyword_researcher.notes"))
        assert len(said) == 1, said
        assert "notes" in said[0] and "string" in said[0], said[0]

    def test_a_human_steps_whole_answer_is_refused(self):
        human = {"id": "ask", "name": "Ask", "type": "human", "human_prompt": "Which keywords?", "output_key": "ask",
                 "human_fields": [{"name": "keywords", "type": "string"}], "next_step_id": "each"}
        said = _issues(_workflow(human, "state.ask"))
        assert len(said) == 1, said
        assert "keywords" in said[0], said[0]

    def test_a_list_field_is_not_refused(self):
        assert _issues(_workflow(_llm([KEYWORDS, NOTES]), "state.keyword_researcher.keywords")) == []

    def test_a_tool_steps_answer_is_not_refused(self):
        """A Tool step says no fields: what it answers may be a list, and the run checks it (F1613's run half)."""
        tool = {"id": "fetch", "name": "Fetch", "type": "tool", "forced_tool": "read_csv", "output_key": "fetch",
                "next_step_id": "each"}
        assert _issues(_workflow(tool, "state.fetch")) == []

    def test_a_models_text_declared_a_list_is_not_refused(self):
        """F14: an undeclared answer typed a list by the state schema is a list when the loop reads it."""
        orch = _workflow(_llm(None), "state.keyword_researcher",
                         state_schema={"keyword_researcher": {"type": "list", "default": []}})
        assert _issues(orch) == []
