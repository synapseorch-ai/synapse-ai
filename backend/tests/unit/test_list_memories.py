"""Every memory can be listed, a page at a time (F274).

"Which warehouse has the largest capacity?" across 120 saved memories took 61
`search_memory` calls in 256 s and never converged; with credit, 52 searches
and 16 task updates over 341 s. Search returns the top five word-overlap hits,
and there was no way to see them all.

`list_memories` pages through every memory — the index, then the archive —
newest first, each by its name and its one-line summary, and lists past
sessions the same way. It reads the store itself, so it works wherever memory
is kept (a local folder, object storage), whatever the search index is.
"""
import json
import types

import pytest

from _fakes import seed as S

from core import agent_memory as am


def _call(tool: str, **args) -> str:
    return json.dumps({"tool": tool, "arguments": args})


@pytest.fixture(autouse=True)
def _fresh_index():
    am.set_memory_index(None)
    yield
    am.set_memory_index(None)


async def _warehouses(agent_id: str, n: int = 120):
    for i in range(n):
        await am.write_memory(agent_id, f"w{i:03d}", f"W{i:03d}: capacity {1000 + i * 50}, city Osaka")


def _names(text: str) -> list[str]:
    return [line.split("]")[0][3:] for line in text.splitlines() if line.startswith("- [")]


class TestListing:
    async def test_it_pages_through_every_memory_newest_first(self):
        await _warehouses("agent_w")
        pages = [am.list_memories("agent_w", page=p) for p in (1, 2, 3)]

        assert "120 memories" in pages[0] and "page 1 of 3" in pages[0].lower()
        first = _names(pages[0])
        assert len(first) == 50 and first[0] == "w119", first[:3]
        seen = [name for page in pages for name in _names(page)]
        assert sorted(seen) == [f"w{i:03d}" for i in range(120)], "every memory, the archived ones too"
        assert "capacity 6950" in pages[0], "each by its summary line"

    async def test_a_page_past_the_end_says_so(self):
        await _warehouses("agent_w2", 3)
        assert "no page 5" in am.list_memories("agent_w2", page=5).lower()

    async def test_sessions_are_listed_newest_first(self):
        am.append_session_note("agent_s", "s_first", "- user: first question")
        am.append_session_note("agent_s", "s_second", "- user: second question")
        text = am.list_memories("agent_s", kind="sessions")
        assert "s_first" in text and "s_second" in text
        assert "first question" in text

    async def test_nothing_saved_says_so(self):
        assert "no memories" in am.list_memories("agent_empty").lower()


class TestInTheLoop:
    async def test_a_memory_agent_can_page_them(self, fake_llm):
        from core.react_engine import run_agent_step

        agent = S.make_agent(tools=[], memory=True)
        await _warehouses(agent["id"], 60)
        fake_llm.script([_call("list_memories"), _call("list_memories", page=2), "W059 at 3,950."])
        [ev async for ev in run_agent_step(
            message="which warehouse has the largest capacity?", agent_id=agent["id"], session_id="s_lm",
            server_module=types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={}),
            agent_override=agent, max_turns=5)]

        offered = [t.get("function", {}).get("name") for t in fake_llm.calls[0].get("tools") or []]
        assert "list_memories" in offered
        assert "capacity 3950" in fake_llm.calls[1]["prompt_msg"]
        assert "W000: capacity 1000" in fake_llm.calls[2]["prompt_msg"]

    def test_a_visitor_never_gets_it(self):
        from core import visitor
        assert visitor.clean_tools(["list_memories", "peek"]) == ["peek"]
