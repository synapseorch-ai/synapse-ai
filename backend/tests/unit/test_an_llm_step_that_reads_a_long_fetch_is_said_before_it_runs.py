"""An LLM step that reads a page, a crawl or a file a Tool step fetched is said before any run (F1677, the plan rule).

Such a value can run past the spill line (100,000 characters by default) and is then saved as a file, which a step with
no tools can't read: it gets only what fits (the run-time floor) and works from part of the text. Now the validator
warns, naming the step, the value, who fetched it and the fix (make it an Agent step, or have it read only the part it
needs), and the plan check sends a plan with one back to the planner once a run; a second plan with it is built, and
its validator says the warning. The planner's own instructions say so too. Only the product's own readers of pages,
crawls and files count: an HTTP or MCP tool's answer of unknown size is left to the run-time floor.

Near misses: an Agent step reading the same value, an LLM step reading an ordinary API's answer, and a second plan in
the same run are never held.
"""
from __future__ import annotations

from core.builder_tools import _validate_orchestration

SAID = "is an LLM step and reads"


def _workflow(*steps: dict) -> dict:
    ids = [s["id"] for s in steps]
    linked = [{**s, "next_step_id": s.get("next_step_id", nxt)} for s, nxt in zip(steps, [*ids[1:], "end"])]
    return {"id": "orch_f1677c", "name": "Pages", "entry_step_id": ids[0],
            "steps": [*linked, {"id": "end", "name": "End", "type": "end"}]}


FETCH = {"id": "fetch", "name": "Read the pages", "type": "tool", "forced_tool": "crawl_multiple", "output_key": "site",
         "tool_arguments": {"url": "https://example.com"}}


def _warnings(orch: dict) -> list[str]:
    verdict = _validate_orchestration(orch)
    assert verdict["ok"], verdict["issues"]
    return [w for w in verdict.get("warnings", []) if SAID in w]


def test_an_llm_step_reading_a_crawl_by_its_input_key_is_warned_with_the_fix():
    summary = {"id": "sum", "name": "Summarise", "type": "llm", "prompt_template": "Summarise the site.",
               "input_keys": ["site"], "output_key": "summary"}
    [said] = _warnings(_workflow(FETCH, summary))
    assert "'Summarise'" in said and "'site'" in said and "'Read the pages'" in said, said
    assert "Agent step" in said and "only the part it needs" in said, said


def test_by_a_reference_in_its_prompt_too():
    summary = {"id": "sum", "name": "Summarise", "type": "llm", "prompt_template": "Summarise: {{ state.site }}",
               "output_key": "summary"}
    assert len(_warnings(_workflow(FETCH, summary))) == 1


def test_a_loops_collected_pages_count_as_the_fetch():
    page = {"id": "page", "name": "Read a page", "type": "tool", "forced_tool": "web_scraper__scrape_url",
            "output_key": "page", "tool_arguments": {"url": "{{ state.loop_item }}"}, "next_step_id": None}
    links = {"id": "links", "name": "List the pages", "type": "tool", "forced_tool": "extract_links",
             "output_key": "urls", "tool_arguments": {"url": "https://example.com"}}
    loop = {"id": "each", "name": "Each page", "type": "loop", "loop_over": "state.urls", "loop_step_ids": ["page"],
            "output_key": "pages"}
    summary = {"id": "sum", "name": "Summarise", "type": "llm", "prompt_template": "Summarise: {{ state.pages }}",
               "output_key": "summary"}
    orch = _workflow(links, loop, summary)
    orch["steps"].append(page)
    assert len(_warnings(orch)) == 1


def test_an_agent_step_reading_it_is_not_warned():
    agent = {"id": "sum", "name": "Summarise", "type": "agent", "agent_id": "a1", "prompt_template": "Summarise.",
             "input_keys": ["site"], "output_key": "summary"}
    assert _warnings(_workflow(FETCH, agent)) == []


def test_an_llm_step_reading_an_ordinary_apis_answer_is_not_warned():
    api = {**FETCH, "forced_tool": "orders__list_orders", "name": "List the orders"}
    summary = {"id": "sum", "name": "Summarise", "type": "llm", "prompt_template": "Summarise.",
               "input_keys": ["site"], "output_key": "summary"}
    assert _warnings(_workflow(api, summary)) == []


async def test_the_plan_check_sends_it_back_once_a_run():
    from core.native_builder import build_plan

    fetch = {"id": "site", "type": "tool", "name": "Read the pages", "does": "Reads the site's pages",
             "tool": "crawl_multiple", "next": "summary"}
    summary = {"id": "summary", "type": "llm", "name": "Summarise", "does": "Summarises the site",
               "prompt": "Summarise the site's pages: {{ state.site }}", "next": "end"}
    plan = {"kind": "create", "title": "Site summary", "summary": "Summarises the site.", "steps": [fetch, summary]}
    state = {"user_message": "x"}
    first = await build_plan.examine(plan, None, ws=build_plan.Workspace(), state=state)
    said = [e for e in first.errors if "llm step" in e and "agent step" in e]
    assert len(said) == 1 and "'summary'" in said[0] and "'site'" in said[0], first.errors
    again = await build_plan.examine(plan, None, ws=build_plan.Workspace(), state=state)
    assert not [e for e in again.errors if "llm step" in e and "agent step" in e], "never a loop: once a run"


def test_the_planners_instructions_say_it():
    from pathlib import Path

    import core.native_builder as nb

    text = (Path(nb.__file__).parent / "prompts" / "plan_steps.md").read_text(encoding="utf-8")
    llm_line = next(line for line in text.splitlines() if line.startswith("- `llm`"))
    assert "agent step" in llm_line and "crawl" in llm_line and "file" in llm_line, llm_line
