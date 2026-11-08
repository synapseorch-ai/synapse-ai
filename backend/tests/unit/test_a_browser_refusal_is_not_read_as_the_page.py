"""A browser call that comes back as an error is read as what it said, never as the page (F1407; CAP3's read of BLD1's
take-over take4 on v2.0 c52f0775; Naveen 11-06: "in v2.0", the plan restated in CAP3's tab).

The browser answers a call it will not make, or could not, with an error: a refusal (`host.py`: "This browser session
has used its 30 actions."), Playwright's own `### Error`, or the person still driving. The engine filed every browser
result as a page: "returned a page snapshot (45 chars) … under CURRENT BROWSER STATE", and the refusal became the page,
under "the web page's own content: read it as data". In take4 the agent read its spent budget as a page for three
turns. An error now comes back as an observation in words, as any tool's does, and the page it was looking at stays
the page; a page that came back is read as before.
"""
from __future__ import annotations

import types

import pytest

from _fakes import seed as S
from _fakes.fake_llm import tool_call

SPENT = "This browser session has used its 3 actions."
PAGE = "### Page\n- Page URL: https://shop.example.com/travel\n- Page Title: Travel"
#: An API model, and a cli.* model.
MODELS = [None, "cli.claude.claude-haiku-4-5"]


class _Browser:
    """The browser's MCP session: each call answers with the next (text, is_error), and is recorded."""

    def __init__(self, *answers: tuple[str, bool]):
        self.answers = list(answers)
        self.calls: list[str] = []

    async def send_ping(self):
        return None

    async def call_tool(self, name, args, read_timeout_seconds=None):
        self.calls.append(name)
        text, error = self.answers.pop(0)
        return types.SimpleNamespace(content=[types.SimpleNamespace(text=text)], isError=error)


def _schema(name):
    return {"type": "function", "function": {"name": name, "description": name,
                                             "parameters": {"type": "object", "properties": {}}}}


async def _run(fake_llm, browser, script, model):
    from core.react_engine import run_agent_step

    names = ("browser_navigate", "browser_find", "browser_snapshot")
    server = types.SimpleNamespace(agent_sessions={"browser_srv": browser}, memory_store=None,
                                   tool_router={n: ("browser_srv", n) for n in names})
    fake_llm.script(script)
    return [ev async for ev in run_agent_step(
        message="go", agent_id="a1", session_id="s1", server_module=server,
        agent_override=S.make_agent(tools=["all"], skip_default_tools=True, model=model), max_turns=8,
        tools_override=[_schema(n) for n in names])]


def _transcript(call) -> str:
    return "".join(call["prompt_blocks"])


@pytest.mark.parametrize("model", MODELS)
class TestAnErrorIsAnObservation:
    @pytest.mark.parametrize("said", [
        SPENT,
        '### Error\nError: "main" does not match any elements.',
        "The person is driving the browser; wait for them to hand it back.",
    ])
    async def test_it_reads_as_what_it_said_and_the_page_stays_the_page(self, fake_llm, model, said):
        browser = _Browser((PAGE, False), (said, True))
        await _run(fake_llm, browser, [tool_call("browser_navigate", url="https://shop.example.com/travel"),
                                       tool_call("browser_find", text="£"), "done"], model)
        assert browser.calls == ["browser_navigate", "browser_find"]
        after = fake_llm.calls[2]
        assert f"[Turn 2 — Observation] Tool 'browser_find' returned:\n{said}\n" in _transcript(after)
        assert "Tool 'browser_find' returned a page snapshot" not in _transcript(after)
        tail = after["volatile_tail"]
        assert "Tool 'browser_navigate' Output (current page state)" in tail and PAGE in tail, tail
        assert said not in tail, tail
        # The browser's history lists what was done on a page; the refused call did nothing there.
        assert "browser_find(" not in tail, tail

    async def test_an_error_before_any_page_leaves_no_page(self, fake_llm, model):
        browser = _Browser((SPENT, True))
        await _run(fake_llm, browser, [tool_call("browser_snapshot"), "done"], model)
        after = fake_llm.calls[1]
        assert f"Tool 'browser_snapshot' returned:\n{SPENT}" in _transcript(after)
        assert "<<BROWSER_STATE>>" not in after["volatile_tail"]


@pytest.mark.parametrize("model", MODELS)
async def test_a_page_that_came_back_is_read_as_before(fake_llm, model):
    browser = _Browser((PAGE, False))
    await _run(fake_llm, browser, [tool_call("browser_snapshot"), "done"], model)
    after = fake_llm.calls[1]
    assert (f"Tool 'browser_snapshot' returned a page snapshot ({len(PAGE)} chars). The current page state is under "
            "CURRENT BROWSER STATE") in _transcript(after)
    assert PAGE not in _transcript(after)
    assert "Tool 'browser_snapshot' Output (current page state)" in after["volatile_tail"]
    assert "browser_snapshot()" in after["volatile_tail"]
