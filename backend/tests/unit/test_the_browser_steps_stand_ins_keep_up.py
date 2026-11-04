"""The tests' stand-ins for the run's browser keep up with what a Browser step calls on it (F1356; CI2 41825833597).

F1356's Browser step began handing the run page the browser's events while its replay runs
(`browser_client.tools.take_events`). The real component has it, on every host — cloud's worker plugs its seams into
the engine's own BrowserTools — but two tests' stand-ins did not, and 14 cases failed on an AttributeError that said
nothing of a browser step. So the calls are read once, from the step's own source, and every stand-in is held to
them: a call a Browser step makes next fails here, by name, not in fourteen places.

And to what the real client does, not only to its names (F1363): F1356's stand-in queued a replay's actions as an
agent's, and the real client queued none, so the run page's promise passed its test and failed on REL3's install. A
stand-in's replay now says only what the real client's says, run against a component whose log grows.
"""
from __future__ import annotations

import inspect
import re

import pytest

from core.browser.client import BrowserTools
from core.orchestration.steps import BrowserStepExecutor

#: What a Browser step calls on the run's browser, read from its source.
STEP_CALLS = frozenset(re.findall(r"(?:browser_client\.)?\btools\.([a-z_]+)\(", inspect.getsource(BrowserStepExecutor)))


def test_every_call_is_the_components_own():
    assert {"replay_for_step", "take_events"} <= STEP_CALLS, STEP_CALLS
    assert not [n for n in STEP_CALLS if not callable(getattr(BrowserTools, n, None))], STEP_CALLS


def _stand_ins():
    from test_a_browser_turn_answered_from_the_run_acts_only_on_a_yes import _Browser as TurnBrowser
    from test_browser_step import _Browser as StepBrowser
    from test_every_browser_use_in_a_run_shows_its_pane import _Tools as PaneTools

    return {"test_browser_step": StepBrowser, "test_a_browser_turn_answered…": TurnBrowser,
            "test_every_browser_use_in_a_run_shows_its_pane": PaneTools}


@pytest.mark.parametrize("name", sorted(_stand_ins()))
def test_each_stand_in_has_every_call(name):
    missing = sorted(n for n in STEP_CALLS if not callable(getattr(_stand_ins()[name], n, None)))
    assert not missing, f"{name}'s stand-in for the run's browser lacks {missing}"


async def _the_real_clients_replay(monkeypatch) -> set[tuple[str | None, str | None]]:
    """What the real client queues for a run's replay, opening its browser first: `(type, actor)` of each event."""
    import test_a_replays_steps_reach_the_run_page as real

    from core.browser import client as browser_client
    from core.browser import kept

    monkeypatch.setattr(kept, "keep", lambda *a: None)
    monkeypatch.setattr(kept, "keep_lines", lambda *a: None)
    monkeypatch.setattr(browser_client, "REPLAY_POLL_S", 0.01)
    host = real._Host()
    tools = browser_client.BrowserTools(hosts=real._Hosts(host))
    host.tools = tools
    await tools.replay_for_step(real.KEY, real.DOC, [], {})
    return {(e.get("type"), e.get("actor")) for e in tools.take_events(real.KEY)}


def _made(name: str):
    answer = {"done": 0, "outputs": {}}
    return {"test_browser_step": lambda c: c(answer), "test_a_browser_turn_answered…": lambda c: c(answer),
            "test_every_browser_use_in_a_run_shows_its_pane": lambda c: c()}[name](_stand_ins()[name])


async def test_the_real_clients_replay_says_its_browser_and_its_own_lines():
    """The baseline the stand-ins are held to: its browser opening, then each of the replay's lines (F1363)."""
    import pytest as _pytest

    with _pytest.MonkeyPatch.context() as mp:
        said = await _the_real_clients_replay(mp)
    assert said == {("browser_session", None), ("browser_action", "replay")}, said


@pytest.mark.parametrize("name", sorted(_stand_ins()))
async def test_each_stand_ins_replay_says_only_what_the_real_clients_does(name, monkeypatch):
    import test_a_replays_steps_reach_the_run_page as real

    allowed = await _the_real_clients_replay(monkeypatch)
    stand_in = _made(name)
    await stand_in.replay_for_step(real.KEY, real.DOC, [], {})
    said = {(e.get("type"), e.get("actor")) for e in stand_in.take_events(real.KEY)}
    assert said <= allowed, f"{name}'s stand-in says {sorted(said - allowed, key=str)}, which the real client never does"
