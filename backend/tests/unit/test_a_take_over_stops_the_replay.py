"""Taking over a replaying browser stops the replay at its next step, and handing it back carries on from there
(F1367, found on F1363; eb 11-04).

A Browser step replays its recording by code, and the run page's pane offers Take over. `take()` gave the person the
wheel, but the replay's loop never looked at it: unless the network held something, it went straight on to its next
step, pressing and typing on the page the person now thought was theirs. Now each step looks at the wheel first. A
step already under way finishes (an action can't stop halfway), and the replay stops before the next one, as the
person's turn: the run parks on it, as on any turn. Handed back, the replay carries on from the step it stopped
before, never the one after it. The pane's take-over dialog says so while a recording drives: it stops after this
step.
"""
from __future__ import annotations

import json
import types

import pytest

STEPS = [{"id": "a", "n": 1, "action": "open", "url": "https://example.com/", "line": "Open example.com"},
         {"id": "b", "n": 2, "action": "open", "url": "https://www.iana.org/", "line": "Open iana.org"},
         {"id": "c", "n": 3, "action": "open", "url": "https://www.iana.org/domains", "line": "Open the domains"}]


class _Page:
    """A page that goes where it is sent, and lets a test act while a step is under way."""

    url = "about:blank"

    def __init__(self):
        self.went: list[str] = []
        self.during: dict[str, object] = {}

    async def goto(self, url, **_kw):
        self.went.append(url)
        self.url = url
        act = self.during.pop(url, None)
        if act is not None:
            act()

    async def screenshot(self, **_kw):
        return b"\xff\xd8"


def _session():
    from core.browser.host import Session

    s = object.__new__(Session)
    s.id, s.log, s.shots, s.viewers, s.last_frame = "bs1367", [], {}, [], None
    s.wheel, s.banner, s.idle, s.handover, s.held, s.recorder = "agent", None, False, None, None, None
    s.dialog, s.select, s.chooser, s.took_at, s.handback = None, None, None, None, None
    s.replaying, s.replay_sends, s.replay_blocked, s.replay_at, s.fixing = None, None, None, None, None
    s.replay_running = False
    s.ctx, s._cdp = None, None
    s.replay_task, s.replay_id, s.replay_answer = None, None, None
    s.page = _Page()
    return s


async def _replay(s):
    return await s.replay(STEPS, {}, unattended=True, recording_id="rec_e014a4ab03517d2ee981")


class TestATakeOver:
    async def test_during_a_step_it_lets_that_step_finish_and_stops_before_the_next(self):
        s = _session()
        s.page.during["https://example.com/"] = s.take  # the person takes over while step 1 loads
        answer = await _replay(s)
        assert s.page.went == ["https://example.com/"], "nothing was done on the page after the take"
        assert answer.get("handover", {}).get("kind") == "took_over", answer
        assert answer.get("at") == 2 and answer.get("step_id") == "b", answer
        assert s.wheel == "person"
        assert [e["target"] for e in s.log if e["actor"] == "replay"] == ["Open example.com"]

    async def test_its_turn_says_what_stopped_and_how_to_carry_on(self):
        s = _session()
        s.page.during["https://example.com/"] = s.take
        reason = (await _replay(s))["handover"]["reason"]
        assert "Open iana.org" in reason and "hand" in reason.lower(), reason

    async def test_during_the_last_step_the_replay_ends_and_the_wheel_stays_the_persons(self):
        s = _session()
        s.page.during["https://www.iana.org/domains"] = s.take
        answer = await _replay(s)
        assert answer.get("done") == 3 and "handover" not in answer, answer
        assert s.wheel == "person"

    async def test_no_take_over_replays_every_step(self):
        s = _session()
        assert (await _replay(s)).get("done") == 3
        assert len(s.page.went) == 3


class _Request:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


async def test_handed_back_it_carries_on_from_the_step_it_stopped_before():
    from core.browser.host import Host

    s = _session()
    s.page.during["https://example.com/"] = s.take
    await _replay(s)
    s.hand_back("")
    host = object.__new__(Host)
    host.sessions = {s.id: s}
    resp = await host.replay_continue(_Request({"session_id": s.id}))
    answer = json.loads(resp.text)
    assert answer.get("done") == 3, answer
    assert s.page.went == ["https://example.com/", "https://www.iana.org/", "https://www.iana.org/domains"], \
        "it carries on from step 2: not step 1 again, not step 3"


async def test_the_browser_step_parks_on_it_and_carries_on_when_it_comes_back(monkeypatch):
    """The run's side: the step parks on the person's turn, and the hand-back carries the replay on."""
    from test_browser_step import _Browser, _flow, _hand_back, _paused, _start, _state, _steps

    from core.browser import client as browser_client
    from core.browser import recordings

    rec = await recordings.create("Find an order", _steps(), by="you")
    took = {"handover": {"kind": "took_over", "reason": "You took over.", "session_id": "bs1"}, "at": 2,
            "step_id": "s2"}
    browser = _Browser(took, {"done": 3, "outputs": {}})
    monkeypatch.setattr(browser_client, "tools", browser)
    events = await _start(_flow(rec["id"]), "run_f1367")
    assert _paused(events) and ("park", "run:run_f1367", "took_over") in browser.calls
    await _hand_back("run_f1367")
    assert browser.calls[-1][0] == "replay" and browser.calls[-1][4] is True
    assert (await _state("run_f1367")).status == "completed"
