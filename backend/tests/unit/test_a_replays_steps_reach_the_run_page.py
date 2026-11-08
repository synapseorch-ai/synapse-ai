"""A Browser step's replay reaches the run page whole: its lines, as it goes, and a picture for each (F1363, an F1356
follow-up; REL3's rc4 browser proof; eb 11-04).

REL3 ran a workflow with one Browser step replaying a 2-step recording. The live pane said "The agent is driving",
with no agent anywhere; the finished run page said "The browser · 1 action" over an empty picture. Only an agent's
own call carried its line into the run's events, kept its screenshot and kept its line (`BrowserTools.call_tool`);
`replay_for_step` only asked how the replay was going. The host offers a replay's lines to whoever watches its
frames, so the run page's own viewer kept the one it saw (step 2: the pane opened after step 1 had run), and no
screenshot at all. F1356's test stood in for the client with one that queued a replay's actions, which the real
client never did.

Now the real client takes the host's new lines at each look at the replay, and once more at its end, and does for
each what an agent's call does: a replay's line is a `browser_action` for the run's events, live, with its screenshot
kept; every line is kept with the run (the guard's, and the person's as text only: nothing is pictured while they
drive). The same for a replay carried on. Nothing waits on anyone watching. A step cancelled mid-replay leaves no
look running and says nothing after its end. And the host says when a replay drives (`replaying`), so the pane says
"The recording is driving", never the agent.
"""
from __future__ import annotations

import asyncio
import types

import pytest

from core.browser import client as browser_client

KEY = "run:run_f1363"
SID = "bs1363"


def _line(n: int, actor: str, action: str, target: str, shot: str | None) -> dict:
    return {"n": n, "at": float(n), "session_id": SID, "actor": actor, "action": action, "target": target,
            "url": "https://www.iana.org/help/example-domains", "shot_ref": shot}


#: Before the replay: an agent's line, which its own call already carried.
BEFORE = [_line(1, "agent", "browser_navigate", "https://example.com", "1")]
#: What the replay writes, poll by poll (REL3's recording: two opens), with a guard's line and the person's.
GROWS = [
    [_line(2, "replay", "open", "Open https://example.com", "2")],
    [_line(3, "replay", "open", "Open https://www.iana.org/help/example-domains", "3"),
     _line(4, "guard", "blocked", "a POST to tracker.example", None),
     _line(5, "person", "pressed", "Accept cookies", "5")],  # a shot ref on the person's line is never fetched
]


class _Host:
    """The browser component: a replay that runs for as many looks as `GROWS` has, its log growing at each."""

    def __init__(self, polls: int | None = None, *, slow: float = 0.0):
        self.log = list(BEFORE)
        self.grows = [list(g) for g in GROWS]
        self.polls = len(self.grows) if polls is None else polls
        self.slow = slow
        self.calls: list[tuple[str, str]] = []
        self.shots: list[str] = []
        self.events_at_look: list[list[int]] = []
        self.tools = None

    async def call(self, method, path, json=None, params=None, timeout=None):
        self.calls.append((method, path))
        if path == "/session":  # a run's first use opens its browser
            return 200, {"session_id": SID}
        if path in ("/replay", "/replay/continue"):
            return 202, {"replay_id": (json or {}).get("replay_id")}
        if path == "/replay/status":
            await asyncio.sleep(self.slow)
            queued = [e.get("n") for e in (self.tools._events.get(KEY) or []) if e.get("type") == "browser_action"]
            self.events_at_look.append(queued)
            if self.grows:
                self.log += self.grows.pop(0)
            self.polls -= 1
            if self.polls >= 0:
                return 200, {"replay_id": params.get("replay_id"), "running": True}
            return 200, {"replay_id": params.get("replay_id"), "running": False, "done": 2, "outputs": {}}
        if path == "/log":
            after = int((params or {}).get("after") or 0)
            return 200, {"entries": [e for e in self.log if e["n"] > after]}
        raise AssertionError(f"{method} {path} is not a replay's")

    async def get_bytes(self, path, params=None, timeout=None):
        assert path == "/shot"
        self.shots.append(params["ref"])
        return 200, b"\xff\xd8" + params["ref"].encode()


class _Hosts:
    def __init__(self, host):
        self.host = host

    async def host_for(self, key, *, opening):
        return self.host


@pytest.fixture
def kept(monkeypatch):
    from core.browser import kept as kept_mod

    out = types.SimpleNamespace(shots={}, lines=[])
    monkeypatch.setattr(kept_mod, "keep", lambda key, ref, jpeg: out.shots.__setitem__(ref, jpeg))
    monkeypatch.setattr(kept_mod, "keep_lines", lambda key, entries: out.lines.extend(entries))
    monkeypatch.setattr(browser_client, "REPLAY_POLL_S", 0.01)
    return out


async def _tools(host: _Host):
    tools = browser_client.BrowserTools(hosts=_Hosts(host))
    host.tools = tools
    await tools.store.set_owner(KEY, {"sid": SID, "parked": None, "sessions": None, "expired": None})
    return tools


DOC = {"id": "rec_e014a4ab03517d2ee981", "name": "Example domains"}


class TestTheRealClient:
    @pytest.mark.parametrize("carry_on", [False, True])
    async def test_each_replay_line_is_an_action_for_the_run_with_its_picture_kept(self, kept, carry_on):
        host = _Host()
        tools = await _tools(host)
        answer = await tools.replay_for_step(KEY, DOC, [], {}, carry_on=carry_on)
        assert answer.get("done") == 2, answer
        actions = [e for e in tools.take_events(KEY) if e.get("type") == "browser_action"]
        assert [(e["n"], e["actor"]) for e in actions] == [(2, "replay"), (3, "replay")], actions
        assert all(e["session_id"] == SID for e in actions)
        assert sorted(kept.shots) == ["2", "3"], kept.shots

    async def test_its_lines_come_while_it_runs_not_only_at_its_end(self, kept):
        host = _Host()
        tools = await _tools(host)
        await tools.replay_for_step(KEY, DOC, [], {})
        assert host.events_at_look[-1][:1] == [2], host.events_at_look

    async def test_every_line_is_kept_with_the_run_the_persons_as_text_only(self, kept):
        host = _Host()
        tools = await _tools(host)
        await tools.replay_for_step(KEY, DOC, [], {})
        assert sorted({e["n"] for e in kept.lines}) == [2, 3, 4, 5], kept.lines
        assert "5" not in kept.shots and "5" not in host.shots, "nothing is pictured while the person drives"
        assert "4" not in kept.shots

    async def test_a_line_from_before_it_is_never_said_again(self, kept):
        host = _Host()
        tools = await _tools(host)
        await tools.replay_for_step(KEY, DOC, [], {})
        assert 1 not in [e.get("n") for e in tools.take_events(KEY)]
        assert "1" not in host.shots

    async def test_only_its_own_lines_are_actions(self, kept):
        host = _Host()
        tools = await _tools(host)
        await tools.replay_for_step(KEY, DOC, [], {})
        actors = {e["actor"] for e in tools.take_events(KEY) if e.get("type") == "browser_action"}
        assert actors == {"replay"}, actors

    async def test_cancelled_mid_replay_no_look_runs_on_and_nothing_is_said_after(self, kept):
        host = _Host(polls=50, slow=0.02)
        tools = await _tools(host)
        task = asyncio.create_task(tools.replay_for_step(KEY, DOC, [], {}), name="browser-replay:f1363")
        for _ in range(100):
            await asyncio.sleep(0.01)
            if any(e.get("type") == "browser_action" for e in tools._events.get(KEY) or []):
                break
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        calls, queued = len(host.calls), len(tools._events.get(KEY) or [])
        await asyncio.sleep(0.2)
        assert len(host.calls) == calls, "a look at the replay ran on after its step was cancelled"
        assert len(tools._events.get(KEY) or []) == queued, "a line was said after the step's end"
        assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]


class _Page:
    url = "https://example.com/"

    def __init__(self, session):
        self.session, self.seen = session, []

    async def goto(self, url, **_kw):
        self.seen.append(self.session.state().get("replaying"))
        if "broken" in url:
            raise RuntimeError("net::ERR_NAME_NOT_RESOLVED")
        self.url = url

    async def screenshot(self, **_kw):
        return b"\xff\xd8"


def _session():
    from core.browser.host import Session

    s = object.__new__(Session)
    s.id, s.log, s.shots, s.viewers, s.last_frame = "bs1363", [], {}, [], None
    s.wheel, s.banner, s.idle, s.handover, s.held, s.recorder = "agent", None, False, None, None, None
    s.dialog, s.select, s.chooser = None, None, None
    s.replaying, s.replay_sends, s.replay_blocked, s.replay_at, s.fixing = None, None, None, None, None
    s.replay_running = False  # as Session.__init__ sets it (F1363)
    s.load_error = None  # as Session.__init__ sets it (F1425)
    s.ctx, s._cdp = None, None
    s.page = _Page(s)
    return s


class TestTheHostSaysWhoDrives:
    async def test_a_replay_says_it_drives_while_it_runs_and_not_after(self):
        s = _session()
        steps = [{"id": "a", "n": 1, "action": "open", "url": "https://example.com", "line": "Open example.com"},
                 {"id": "b", "n": 2, "action": "open", "url": "https://www.iana.org/", "line": "Open iana.org"}]
        done = await s.replay(steps, {}, unattended=True, recording_id=DOC["id"])
        assert done.get("done") == 2, done
        assert s.page.seen == [True, True], s.page.seen
        assert s.state().get("replaying") is False

    def test_a_browser_nobody_replays_in_says_so(self):
        assert _session().state().get("replaying") is False

    async def test_a_replay_stopped_at_a_miss_drives_no_more(self):
        """What follows a miss is the agent's fix or the person's turn: neither is the recording."""
        s = _session()
        steps = [{"id": "a", "n": 1, "action": "open", "url": "https://broken.example", "line": "Open broken"}]
        stopped = await s.replay(steps, {}, unattended=True, recording_id=DOC["id"])
        assert stopped.get("miss"), stopped
        assert s.page.seen == [True] and s.state().get("replaying") is False
