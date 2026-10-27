"""
A plan uses what this workspace has before anything is looked up (F950, F1043; SAI2-P3, D300; Naveen 10-24).

Found on prod (rc4.3's check in Kestrel, the old route): "scrape example.com every morning and summarise what changed"
was drafted with `web_scraper` (the built-in scraper *server's* name, not a tool), `natural_language_processing` and
`store_result`. All three read as missing; research offered Apify, a cloud scraper and Google's language API, and
Connect cards followed. In the gethub conversation Synapse AI kept suggesting Firecrawl though the product scrapes.

In the plan check, before any lookup or card, a name nothing provides goes back to the planner when code can say what
it is instead:
- a tool server's name written as a tool: "that is a tool server; its tools are …: name one" (F1043 a);
- a model's own work written as a tool (summarise, classify, store the result, natural language processing…):
  "that is a model step, not a tool; a step's output is kept in the run's state" (F1043 b);
- a job the built-in scraper does (scrape, crawl, read a page) under another service's name the person never named:
  "use the built-in `scrape_url`", unless the person named that service (F950: built-in always, unless named).
Research runs only for a service the person named, or a real gap.
"""
from __future__ import annotations

import json

import pytest

from core import builder_access
from core.builder_tools import execute_builder_tool_with_events
from core.models_orchestration import OrchestrationRun
from core.native_builder import build_plan
from core.orchestration.engine import live
from core.orchestration.state import SharedState
from core.store.resources import save_tool

SCRAPE_URL = {"name": "scrape_url", "description": "Reads a web page and returns its text.", "tool_type": "http",
              "method": "GET", "url": "https://scraper.internal.example/scrape", "headers": {},
              "inputSchema": {"type": "object", "properties": {"url": {"type": "string"}}},
              "annotations": {"readOnlyHint": True}}


class _Server:
    agent_sessions: dict = {}
    tool_router: dict = {"web_scraper__scrape_url": ("web_scraper", "scrape_url"),
                         "web_scraper__crawl_multiple": ("web_scraper", "crawl_multiple")}


@pytest.fixture(autouse=True)
async def scraper():
    await save_tool(dict(SCRAPE_URL))
    builder_access.forget_runs()
    yield
    builder_access.forget_runs()


async def _call(tool, args, message, server=None):
    run = OrchestrationRun(run_id="builder_here", orchestration_id="orch_native_builder",
                           shared_state={builder_access.CALLER_KEY: {"user_id": "u1"}, "intent": "create",
                                         "user_message": message})
    await SharedState(run).checkpoint()
    run.shared_state[build_plan.DRAFT_KEY] = args.get("draft") or args
    with live(run):
        raw, events = await execute_builder_tool_with_events(tool, args, server or _Server(), run_id=run.run_id)
    return (json.loads(raw) if isinstance(raw, str) else raw), events


def _plan(*tools):
    steps = [{"id": f"s{i}", "type": "tool", "name": f"Step {i}", "does": f"Does {t}", "tool": t}
             for i, t in enumerate(tools)]
    return {"kind": "create", "title": "Morning changes", "summary": "Scrapes example.com and says what changed.",
            "steps": steps}


SCRAPE = "Scrape example.com every morning and summarise what changed"


async def test_kestrels_three_names_go_back_once_with_no_lookup_and_no_card():
    sent, _ = await _call("submit_plan", _plan("web_scraper", "natural_language_processing", "store_result"), SCRAPE)
    error = sent["error"]
    assert "`web_scraper` is a tool server" in error and "scrape_url" in error and "crawl_multiple" in error
    assert "`natural_language_processing` is a model's own work" in error
    assert "`store_result` is a model's own work" in error and "kept in the run's state" in error


async def test_a_misnamed_plan_that_reaches_the_check_never_researches_them():
    draft = {"finish": "submit_plan", **_plan("web_scraper", "store_result")}
    out, events = await _call("check_build", {"draft": draft}, SCRAPE)
    assert out["outcome"] != "research" and not out.get("research")
    assert not [e for e in events if e.get("type") == "connection_needed"]


async def test_a_job_the_built_in_scraper_does_goes_back_to_it_when_no_other_service_was_named():
    sent, _ = await _call("submit_plan", _plan("firecrawl_scrape"), SCRAPE)
    assert "`firecrawl_scrape`" in sent["error"] and "`scrape_url`" in sent["error"]
    assert "built in" in sent["error"]


async def test_a_service_the_person_named_is_planned_and_looked_up():
    draft = {"finish": "submit_plan", **_plan("firecrawl_scrape")}
    sent, _ = await _call("submit_plan", _plan("firecrawl_scrape"), "Use Firecrawl to scrape example.com every morning")
    assert sent.get("finish") == "submit_plan", sent
    out, _ = await _call("check_build", {"draft": draft}, "Use Firecrawl to scrape example.com every morning")
    assert out["outcome"] == "research" and out["research"] == ["firecrawl_scrape"]


async def test_the_built_in_is_used_as_is():
    draft = {"finish": "submit_plan", **_plan("scrape_url")}
    out, events = await _call("check_build", {"draft": draft}, SCRAPE)
    assert out["outcome"] == "review" and [t["status"] for t in out["plan"]["tools"]] == ["present"]
    assert not [e for e in events if e.get("type") == "connection_needed"]


async def test_an_outside_service_with_no_built_in_for_its_job_still_researches():
    draft = {"finish": "submit_plan", **_plan("send_mailchimp_campaign")}
    out, _ = await _call("check_build", {"draft": draft}, "Send the newsletter with Mailchimp every Friday")
    assert out["outcome"] == "research" and out["research"] == ["send_mailchimp_campaign"]


async def test_a_connected_servers_name_as_a_tool_lists_its_tools():
    class _Firecrawl(_Server):
        tool_router = {"firecrawl__firecrawl_scrape": ("ext_mcp_firecrawl", "firecrawl_scrape"),
                       "firecrawl__firecrawl_crawl": ("ext_mcp_firecrawl", "firecrawl_crawl")}

    sent, _ = await _call("submit_plan", _plan("firecrawl"), "Use Firecrawl to scrape example.com", _Firecrawl())
    assert "`firecrawl` is a tool server" in sent["error"]
    assert "firecrawl_scrape" in sent["error"] and "firecrawl_crawl" in sent["error"]


@pytest.mark.parametrize("name", ["summarize_text", "classify_sentiment", "save_results", "analyse_changes",
                                  "generate_summary"])
def test_a_models_own_work_is_read_in_code(name):
    assert build_plan.model_work(name)


@pytest.mark.parametrize("name", ["send_email", "save_to_google_sheet", "store_in_airtable", "post_slack_message",
                                  "send_mailchimp_campaign", "scrape_url"])
def test_a_tool_that_acts_somewhere_is_not_model_work(name):
    assert not build_plan.model_work(name)


# ── SAI2's change 3: "the built-in can't do it" is a code fact, never a model's judgement ─────────────────────────

@pytest.mark.parametrize("message", [
    "Scrape these 5,000 product pages every night through rotating proxies",
    "Scrape the site's pages that only load with JavaScript rendering, every morning",
])
async def test_a_need_the_built_ins_own_description_lacks_lets_another_service_be_looked_up(message):
    draft = {"finish": "submit_plan", **_plan("firecrawl_scrape")}
    sent, _ = await _call("submit_plan", _plan("firecrawl_scrape"), message)
    assert sent.get("finish") == "submit_plan", sent
    out, _ = await _call("check_build", {"draft": draft}, message)
    assert out["outcome"] == "research"


async def test_a_need_the_built_in_says_it_meets_keeps_the_built_in():
    """The same words, where the built-in's own description covers them, stay with the built-in."""
    await save_tool({**SCRAPE_URL, "description": "Reads a web page, rendering its JavaScript, and returns its text."})
    sent, _ = await _call("submit_plan", _plan("firecrawl_scrape"),
                          "Scrape the site's pages that only load with JavaScript rendering, every morning")
    assert "`scrape_url`" in sent["error"]


def test_the_needs_beyond_a_built_in_are_one_list_in_code():
    assert {"proxy", "javascript", "thousands"} <= set(build_plan.SCRAPE_BEYOND)
