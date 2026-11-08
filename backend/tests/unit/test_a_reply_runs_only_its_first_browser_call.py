"""A reply's first browser call is the last thing it does (F1408; CAP3's read of BLD1's take-over take4 on v2.0
c52f0775; Naveen 11-06: "in v2.0", the plan restated in CAP3's tab, and no call after it runs, a browser one or not).

A model that writes its calls as text gets nothing back inside a reply. In take4 a cli.* model wrote 54 calls in one
reply, with prose between them on results it never had ("Here is the snapshot…", "the snapshots keep timing out"); the
engine ran every call, the browser's 30 actions were spent in one turn, and the calls written before the person handed
the browser back drove it away from the page they handed back. The page changes with each browser call, and only the
last one is shown whole, so what a reply wrote after its first browser call was written without its result: it is not
kept, its calls are not made, and one engine line names them. A call before it runs, and a reply with one browser call
and nothing after it, or none, is read exactly as before.
"""
from __future__ import annotations

import types

import pytest

from _fakes import seed as S
from _fakes.fake_llm import tool_call

PAGE = "### Page\n- Page URL: https://shop.example.com/travel"
NAV = tool_call("browser_navigate", url="https://shop.example.com/travel")
FIND = tool_call("browser_find", text="£")
LIST = tool_call("list_files")
MINE = tool_call("my_tool", x=1)
CUT = "System: that reply went on after its first browser call, before that call's result was back"
CLI = "cli.claude.claude-haiku-4-5"


def _wrapped(call: str) -> str:
    return f"<tool_call>{call}</tool_call>"


class _Browser:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def send_ping(self):
        return None

    async def call_tool(self, name, args, read_timeout_seconds=None):
        self.calls.append((name, args))
        return types.SimpleNamespace(content=[types.SimpleNamespace(text=PAGE)], isError=False)


def _schema(name):
    return {"type": "function", "function": {"name": name, "description": name,
                                             "parameters": {"type": "object", "properties": {}}}}


async def _run(fake_llm, reply: str, model=None):
    """One reply, then "done". What the browser and the other tools were asked, and the next call's transcript."""
    from core.react_engine import run_agent_step

    browser, executed = _Browser(), []

    async def executor(name, args):
        if name.startswith("browser_"):
            return None  # the browser's own session runs it
        executed.append(name)
        return f"ran-{name}"

    names = ("browser_navigate", "browser_find")
    server = types.SimpleNamespace(agent_sessions={"browser_srv": browser}, memory_store=None,
                                   tool_router={n: ("browser_srv", n) for n in names})
    fake_llm.script([reply, "done"])
    [ev async for ev in run_agent_step(
        message="go", agent_id="a1", session_id="s1", server_module=server,
        agent_override=S.make_agent(tools=["all"], skip_default_tools=True, model=model), max_turns=8,
        tools_override=[_schema(n) for n in (*names, "list_files", "my_tool")], tool_executor=executor)]
    return [name for name, _ in browser.calls], executed, "".join(fake_llm.calls[1]["prompt_blocks"])


class TestTake4sReply:
    async def test_a_cli_models_reply_with_prose_on_results_it_never_had(self, fake_llm):
        reply = ("Let me open the Travel shelf.\n" + _wrapped(NAV) + "\nHere is the snapshot with every price.\n"
                 + _wrapped(FIND) + "\n" + _wrapped(LIST) + "\nThe cheapest is £10.60, with 19 in stock.")
        browser, executed, transcript = await _run(fake_llm, reply, CLI)
        assert browser == ["browser_navigate"] and executed == []
        assert f"[Turn 1 — Action]\nLet me open the Travel shelf.\n{_wrapped(NAV)}\n" in transcript, transcript
        for never_had in ("Here is the snapshot", "£10.60", FIND, LIST):
            assert never_had not in transcript, never_had
        assert f"{CUT}, so nothing after it was kept and these calls were not made: browser_find, list_files." \
            in transcript, transcript

    async def test_an_api_models_calls(self, fake_llm):
        browser, executed, transcript = await _run(fake_llm, "\n".join([NAV, FIND, LIST]))
        assert browser == ["browser_navigate"] and executed == []
        assert f"[Turn 1 — Action]\n{NAV}\n" in transcript and FIND not in transcript and LIST not in transcript
        assert "not made: browser_find, list_files." in transcript

    async def test_words_after_the_call_alone(self, fake_llm):
        browser, _executed, transcript = await _run(fake_llm, _wrapped(NAV) + "\nThe page lists eleven books.", CLI)
        assert browser == ["browser_navigate"]
        assert f"[Turn 1 — Action]\n{_wrapped(NAV)}\n" in transcript and "eleven books" not in transcript
        assert f"{CUT}, so nothing after it was kept." in transcript and "not made" not in transcript

    async def test_a_fenced_call_is_cut_where_it_was_read(self, fake_llm):
        """The cut is made in the text the calls were read from: the reply with its fences taken off."""
        reply = ("Let me look.\n<tool_call>\n```json\n" + NAV + "\n```\n</tool_call>\nThe page shows £5.\n"
                 + _wrapped(FIND))
        browser, _executed, transcript = await _run(fake_llm, reply, CLI)
        assert browser == ["browser_navigate"]
        assert f"[Turn 1 — Action]\nLet me look.\n<tool_call>\n\n{NAV}\n\n</tool_call>\n\n" in transcript, transcript
        assert "£5" not in transcript and FIND not in transcript


class TestRunAsBefore:
    async def test_a_call_before_the_first_browser_call(self, fake_llm):
        browser, executed, transcript = await _run(fake_llm, "\n".join([MINE, NAV, FIND]))
        assert executed == ["my_tool"] and browser == ["browser_navigate"]
        assert f"[Turn 1 — Action]\n{MINE}\n{NAV}\n" in transcript and "not made: browser_find." in transcript

    @pytest.mark.parametrize("reply, model", [
        (NAV, None),                                                         # an API model's one call
        ("Let me open the Travel shelf.\n" + _wrapped(NAV), CLI),            # a cli.* model's
        ("I'll read it.\n" + _wrapped(NAV) + "\n\n", CLI),                   # with nothing but space after
    ])
    async def test_a_reply_with_one_browser_call_is_kept_as_it_was(self, fake_llm, reply, model):
        browser, _executed, transcript = await _run(fake_llm, reply, model)
        assert browser == ["browser_navigate"]
        assert (f"[Turn 1 — Action]\n{reply}\n\n[Turn 1 — Observation] Tool 'browser_navigate' returned a page "
                "snapshot") in transcript, transcript
        assert "System:" not in transcript

    @pytest.mark.parametrize("model", [None, CLI])
    async def test_a_reply_with_no_browser_call(self, fake_llm, model):
        reply = "\n".join([MINE, LIST]) if model is None else f"{_wrapped(MINE)}\nThen the files.\n{_wrapped(LIST)}"
        browser, executed, transcript = await _run(fake_llm, reply, model)
        assert browser == [] and executed == ["my_tool", "list_files"]
        assert f"[Turn 1 — Action]\n{reply}\n" in transcript and "System:" not in transcript
