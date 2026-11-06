"""A page the person hands back is read before anything changes it (F1417; BLD1's take-over take5 on v2.0 9649b4cd;
Naveen 11-06, in eb's tab and CAP3's: fix in FF23, only a hand-back holds a call).

In take5 the agent's second navigate (to a Travel book) was written while the person took the wheel, went to Poetry and
handed it back: "Poetry instead". The call reached the browser after the hand-back, ran on the person's page and took
the browser away from it, and its result came back headed by the hand-back's note, "The page is now …poetry…", above a
page that was the Travel book's. The agent was told something untrue, and the person's page was lost. Now, after a
take-over the person made themselves, the first call after the hand-back that would change the page is not made: the
agent hears the note with the page as the person left it, and acts on it next. A call that reads runs, with the note
before it, as before; a release or an allowed site says its line and the call runs, as before, since the person did not
drive; and a turn the agent asked for, or Synapse gave the person, stopped the agent's turn, so the call it makes after
the hand-back runs, as before (Naveen and eb, on CI2's run of FF23: `test_an_agents_send_waits_for_the_person`).
"""
from __future__ import annotations

import asyncio
import types

import pytest

from core.browser import gate
from core.browser.host import Session

TRAVEL = "https://books.toscrape.com/catalogue/category/books/travel_2/index.html"
POETRY = "https://books.toscrape.com/catalogue/category/books/poetry_23/index.html"
BOOK = "https://books.toscrape.com/catalogue/the-road-to-little-dribbling_277/index.html"


class _Page:
    def __init__(self, url):
        self.url = url
        self.frames = []

    async def screenshot(self, **_kw):
        return b"\xff\xd8"


class _Mcp:
    """Playwright MCP: a navigate goes where it is sent; every call answers with the page it is on."""

    def __init__(self, page):
        self.page = page
        self.calls: list[str] = []

    async def call_tool(self, name, args):
        self.calls.append(name)
        if name == "browser_navigate":
            self.page.url = args["url"]
        text = f"### Page\n- Page URL: {self.page.url}"
        if name == "browser_evaluate":  # a field's type, or what a press lands on: a link to a shelf
            text = ('["password", ""]' if "el.type" in args["function"]
                    else '### Result\n{"label": "Poetry", "tag": "a", "href": "/poetry_23/index.html"}')
        return types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text=text)], isError=False)


def _session(tmp_path) -> Session:
    s = Session("bs1417", "o", gate.CO_DRIVE, {}, tmp_path, sites=["books.toscrape.com"])
    s.page = _Page(TRAVEL)
    s.mcp = _Mcp(s.page)
    return s


def _handed_back(s: Session, note: str = "Poetry instead, please: the cheapest one here.") -> None:
    s.take()
    s.page.url = POETRY  # the person drove there
    s.hand_back(note)


class TestTake5:
    async def test_the_agents_navigate_is_not_made_and_it_reads_the_persons_page(self, tmp_path):
        s = _session(tmp_path)
        _handed_back(s)
        out = await s.call("browser_navigate", {"url": BOOK})
        assert s.mcp.calls == ["browser_snapshot"], "the page the person left is read, nothing else is done"
        assert s.page.url == POETRY
        text = out["text"]
        assert text.startswith("### The person\nThe person drove for "), text
        assert 'said: "Poetry instead, please: the cheapest one here."' in text and f"The page is now {POETRY}" in text
        assert "Your browser_navigate was not made" in text, text
        assert text.endswith(f"### Page\n- Page URL: {POETRY}"), text
        assert BOOK not in text
        assert out["is_error"] is False and out["log"] is None
        assert s.actions == 0, "a call not made spends none of the session's actions"
        assert not [e for e in s.log if e["actor"] == "agent"]

    async def test_the_next_call_is_made(self, tmp_path):
        s = _session(tmp_path)
        _handed_back(s)
        await s.call("browser_navigate", {"url": BOOK})
        out = await s.call("browser_navigate", {"url": BOOK})
        assert s.mcp.calls == ["browser_snapshot", "browser_navigate"] and s.page.url == BOOK
        assert "### The person" not in out["text"] and "not made" not in out["text"]
        assert s.actions == 1


@pytest.mark.parametrize("name, args", [
    ("browser_click", {"element": "Add to basket", "target": "e39"}),
    ("browser_type", {"element": "Quantity", "target": "e38", "text": "2", "submit": True}),
    ("browser_press_key", {"key": "Enter"}),
    ("browser_select_option", {"element": "Sort", "target": "e9", "values": ["price"]}),
    ("browser_navigate_back", {}),
    ("browser_tabs", {"action": "select", "index": 0}),
])
async def test_every_call_that_changes_the_page_waits(tmp_path, name, args):
    assert name in gate.CHANGES_PAGE
    s = _session(tmp_path)
    _handed_back(s)
    out = await s.call(name, args)
    assert s.mcp.calls == ["browser_snapshot"], s.mcp.calls
    assert f"Your {name} was not made" in out["text"] and "### The person" in out["text"]


@pytest.mark.parametrize("name, args", [("browser_snapshot", {}), ("browser_find", {"text": "£"}),
                                        ("browser_wait_for", {"time": 1})])
async def test_a_call_that_reads_runs_with_the_note_before_it(tmp_path, name, args):
    s = _session(tmp_path)
    _handed_back(s)
    out = await s.call(name, args)
    assert s.mcp.calls == [name]
    assert out["text"].startswith("### The person\n") and f"- Page URL: {POETRY}" in out["text"]
    assert "not made" not in out["text"]
    assert s.handback is None


class TestATurnTheAgentGave:
    """Only a take-over the person made themselves holds a call (Naveen and eb, 11-06, on CI2's run of FF23): a turn the
    agent asked for, or Synapse gave the person, stopped the agent's turn, so its next call is written after it."""

    async def test_a_password_then_the_hand_back_then_the_click_runs(self, tmp_path):
        s = _session(tmp_path)
        pw = await s.call("browser_type", {"element": "Password", "target": "e12", "text": "x"})
        assert pw["handover"]["kind"] == "sensitive" and s.wheel == "person"
        s.hand_back("")
        out = await s.call("browser_click", {"element": "Poetry", "target": "e7"})
        assert s.mcp.calls[-1] == "browser_click" and "not made" not in out["text"], (s.mcp.calls, out["text"])
        assert out["text"].startswith("### The person\nThe person drove for ")

    @pytest.mark.parametrize("kind", ["allow_typing", "captcha", "asked"])
    async def test_its_other_turns(self, tmp_path, kind):
        s = _session(tmp_path)
        s._turn(kind, "Yours.")
        s.page.url = POETRY
        s.hand_back("")
        out = await s.call("browser_navigate", {"url": BOOK})
        assert s.mcp.calls == ["browser_navigate"] and s.page.url == BOOK and "not made" not in out["text"]

    async def test_a_held_send_the_person_took_to_press_themselves(self, tmp_path):
        s = _session(tmp_path)
        s._turn("send", "The agent's press would send an order.")
        s.take()
        s.hand_back("")
        out = await s.call("browser_navigate", {"url": BOOK})
        assert s.mcp.calls == ["browser_navigate"] and "not made" not in out["text"]

    async def test_a_take_over_between_the_agents_turns_is_the_persons_own(self, tmp_path):
        s = _session(tmp_path)
        s.go_idle()
        _handed_back(s)
        out = await s.call("browser_navigate", {"url": BOOK})
        assert s.mcp.calls == ["browser_snapshot"] and "Your browser_navigate was not made" in out["text"]


class TestNotAHandBack:
    """The person did not drive: the call runs, with their line before it, as before (Naveen: only a hand-back)."""

    async def test_a_release(self, tmp_path):
        s = _session(tmp_path)
        s.held = {"method": "POST", "url": "https://books.toscrape.com/api", "event": asyncio.Event(),
                  "decision": None, "money": False}
        s.wheel = "nobody"
        assert s.release() == {"released": True}
        s.held = None
        out = await s.call("browser_navigate", {"url": BOOK})
        assert s.mcp.calls == ["browser_navigate"] and s.page.url == BOOK
        assert out["text"].startswith("### The person\nThe person let it go")

    async def test_an_allowed_site(self, tmp_path):
        s = _session(tmp_path)
        s.banner, s.wheel = {"kind": "allow_typing", "reason": "", "host": "books.toscrape.com"}, "person"
        s.allow_site("books.toscrape.com")
        out = await s.call("browser_navigate", {"url": BOOK})
        assert s.mcp.calls == ["browser_navigate"] and s.page.url == BOOK
        assert out["text"].startswith("### The person\nThe person allowed typing")
