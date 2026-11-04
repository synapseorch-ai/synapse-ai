"""The tests' stand-ins for the run's browser keep up with what a Browser step calls on it (F1356; CI2 41825833597).

F1356's Browser step began handing the run page the browser's events while its replay runs
(`browser_client.tools.take_events`). The real component has it, on every host — cloud's worker plugs its seams into
the engine's own BrowserTools — but two tests' stand-ins did not, and 14 cases failed on an AttributeError that said
nothing of a browser step. So the calls are read once, from the step's own source, and every stand-in is held to
them: a call a Browser step makes next fails here, by name, not in fourteen places.
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
