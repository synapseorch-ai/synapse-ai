"""Every tool name the builder's own listings print is a tool its checks find (F1035).

Prod rc4.2, gethubservice: the plan used the built-in scraper as `web_scraper.scrape_url`, and the plan check said
"Nothing in this workspace provides `web_scraper.scrape_url`", so research ran and offered to connect Firecrawl.
Once Firecrawl was connected, `list_server_tools` printed its tools bare (`firecrawl_scrape`); the catalogue, the
resolver and every saved agent call them `firecrawl__firecrawl_scrape`, so `get_tools_detail` and the plan check
missed them too, and research offered Firecrawl again.

One resolver now reads every spelling the listings teach: the name, `server.tool`, `server__tool`, and a bare
name exactly one server answers. A built-in's own name always wins, so an external server never takes over a
built-in's bare name. What the builder saves is the name a run calls.
"""
from __future__ import annotations

import json
import types

import pytest

from core.builder_tools import execute_builder_tool
from core.tool_router import SEP, ToolRouter


class _Tool:
    def __init__(self, name, description=""):
        self.name = name
        self.description = description or f"{name}."
        self.inputSchema = {"type": "object", "properties": {}}
        self.annotations = None


class _Session:
    def __init__(self, *names):
        self._tools = [_Tool(n) for n in names]

    async def list_tools(self):
        return type("Result", (), {"tools": self._tools})()


@pytest.fixture
def server():
    """The built-in scraper, a connected Firecrawl, and two external servers that share a bare name."""
    sessions = {
        "web_scraper": _Session("scrape_url", "crawl_multiple"),
        "ext_mcp_firecrawl": _Session("firecrawl_scrape", "firecrawl_search"),
        "ext_mcp_alpha": _Session("search_records"),
        "ext_mcp_beta": _Session("search_records"),
        "ext_mcp_shadow": _Session("crawl_multiple"),  # a tenant server answering a built-in's bare name
    }
    router = ToolRouter()
    for key, session in sessions.items():
        external = key.startswith("ext_mcp_")
        namespace = key[len("ext_mcp_"):] if external else key
        for t in session._tools:
            router.register(namespace, t.name, session_key=key, alias=not external)
    return types.SimpleNamespace(agent_sessions=sessions, tool_router=router, memory_store=None, _session_tools={})


async def _call(tool: str, args: dict, server) -> dict | list:
    return json.loads(await execute_builder_tool(tool, args, server))


class TestOneResolver:
    @pytest.mark.parametrize("written, called", [
        ("scrape_url", "scrape_url"),
        ("web_scraper.scrape_url", "scrape_url"),
        (f"firecrawl{SEP}firecrawl_scrape", f"firecrawl{SEP}firecrawl_scrape"),
        ("firecrawl.firecrawl_scrape", f"firecrawl{SEP}firecrawl_scrape"),
        ("firecrawl_scrape", f"firecrawl{SEP}firecrawl_scrape"),
    ])
    async def test_each_spelling_the_listings_teach_is_the_name_a_run_calls(self, server, written, called):
        from core.builder_guard import canonical_names, missing

        assert await canonical_names([written], server) == [called]
        assert await missing([written], server) == []

    async def test_a_builtins_bare_name_is_never_taken_by_an_external_server(self, server):
        from core.builder_guard import canonical_names

        assert await canonical_names(["crawl_multiple"], server) == ["crawl_multiple"]

    async def test_a_bare_name_two_servers_answer_is_not_guessed(self, server):
        from core.builder_guard import canonical_names, missing

        assert await canonical_names(["search_records"], server) == ["search_records"]
        (gone, near), = await missing(["search_records"], server)
        assert gone == "search_records" and set(near) >= {f"alpha{SEP}search_records", f"beta{SEP}search_records"}

    async def test_a_name_nothing_provides_is_still_missing(self, server):
        from core.builder_guard import missing

        assert [n for n, _ in await missing(["firecrawl_crawl_everything", "web_scraper.nope"], server)] == [
            "firecrawl_crawl_everything", "web_scraper.nope"]


class TestTheListingsAndTheChecksAgree:
    async def test_every_name_list_server_tools_prints_is_found_by_the_plan_check_and_the_detail(self, server):
        from core.builder_guard import check_plan

        for srv in ("web_scraper", "firecrawl", "alpha"):
            printed = [t["name"] for t in await _call("list_server_tools", {"server_name": srv}, server)]
            detail = await _call("get_tools_detail", {"tool_names": printed}, server)
            assert not detail.get("_not_found"), (srv, detail)
            plan = "## Steps\n" + "\n".join(f"1. Use `{n}`." for n in printed)
            assert (await check_plan(plan, server))["missing_tools"] == [], (srv, printed)

    async def test_an_external_servers_tools_are_printed_as_a_run_calls_them(self, server):
        printed = [t["name"] for t in await _call("list_server_tools", {"server_name": "firecrawl"}, server)]
        assert printed == [f"firecrawl{SEP}firecrawl_scrape", f"firecrawl{SEP}firecrawl_search"]
        builtin = [t["name"] for t in await _call("list_server_tools", {"server_name": "web_scraper"}, server)]
        assert builtin == ["scrape_url", "crawl_multiple"], "a built-in's tools stay bare"

    async def test_get_tools_detail_finds_the_bare_name_list_server_tools_used_to_print(self, server):
        detail = await _call("get_tools_detail", {"tool_names": ["firecrawl_scrape", "web_scraper.scrape_url"]},
                             server)
        assert detail["firecrawl_scrape"]["name"] == f"firecrawl{SEP}firecrawl_scrape"
        assert detail["web_scraper.scrape_url"]["name"] == "scrape_url" and not detail.get("_not_found")

    async def test_gethubservices_plan_needs_no_research(self, server):
        """The prod case: the plan names the built-in scraper as `web_scraper.scrape_url`."""
        from core.builder_guard import check_plan

        plan = ("## Steps\n1. **Scrape the listing** (agent: Lead finder) uses `web_scraper.scrape_url`.\n"
                "## New agents\n- Lead finder: tools `web_scraper.scrape_url`, `firecrawl_search`")
        out = await check_plan(plan, server)
        assert out["missing_tools"] == [] and not out.get("research") and not out.get("redraft")


class TestWhatTheBuilderSaves:
    async def test_an_agent_is_saved_with_the_names_a_run_calls(self, server):
        made = await _call("create_agent", {"name": "Lead finder", "description": "Finds leads.",
                                            "type": "conversational", "system_prompt": "Find leads.",
                                            "tools": ["web_scraper.scrape_url", "firecrawl_search"]}, server)
        assert "error" not in made, made
        from core.routes.agents import load_user_agents

        saved = next(a for a in await load_user_agents() if a.get("name") == "Lead finder")
        assert saved["tools"] == ["scrape_url", f"firecrawl{SEP}firecrawl_search"]

    async def test_a_tool_step_is_saved_with_the_name_a_run_calls(self, server):
        made = await _call("create_orchestration", {
            "name": "Scrape daily", "description": "d", "entry_step_id": "s1",
            "steps": [{"id": "s1", "name": "Scrape", "type": "tool", "forced_tool": "web_scraper.scrape_url",
                       "next_step_id": "end"}, {"id": "end", "name": "End", "type": "end"}]}, server)
        assert "error" not in made, made
        from core.routes.orchestrations import load_orchestrations

        orch = next(o for o in await load_orchestrations() if o.get("name") == "Scrape daily")
        step = next(s for s in orch["steps"] if s["id"] == "s1")
        assert step["forced_tool"] == "scrape_url"
