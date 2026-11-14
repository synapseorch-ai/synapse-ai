"""Every place that steps Synapse AI's model up, or budgets a step for its model, reads the model's window (F1591).

F1586 (TRI3, prod 11-13): Synapse AI on Bedrock's Mistral Large 24.02 (32,768 tokens) was refused at its planner's
first call, about 40-50k tokens; F1586 kept such a model out of Synapse AI's picker and refused a turn on one. The same
class had more doors:

- "Try again on a stronger model" (F1018, its offer and its press) and the effort offer's "Use <stronger>" (F1153)
  step Synapse AI up with `models_menu.stronger_than`, which never weighed the window: the next model up could be
  one too small for the planner. So could the plan card's stronger model for an agent that reads long inputs, though
  its own pick weighed it (F1251);
- a step's context budget (D339) read a model by the id it was given, and a host's alias can hide the model: cloud's
  `synapse.mistral-large` is the 32K 2402 model, which the table's `mistral-large` row (Mistral's own API) reads as
  128K, so a step on it compacted at about 60k tokens and was refused first. A host now says a model's window
  (`windows.set_host_windows`), kept for a few minutes: a model's window changes only when the catalogue does, and
  a step asks for it every time.
"""
from __future__ import annotations

import types

import pytest

from core import builder_tools  # noqa: F401 — loads the builder's modules first, as at boot
from core import step_refs, try_stronger
from core.config import load_settings
from core.context_budget import budget
from core.models_orchestration import OrchestrationRun, StepConfig
from core.native_builder import effort
from core.native_builder import models_menu as MM
from core.native_builder import objects as O
from core.orchestration import context as context_mod
from core.orchestration import resolved as resolved_mod
from core.orchestration import steps as steps_mod
from core.providers import windows

#: The table reads this alias by its `mistral-large` row (Mistral's own API, 128K); the host knows it is Bedrock's 32K
#: 2402 model, as cloud's catalogue does.
ALIAS = "synapse.mistral-large"
SMALL_WINDOW = 32_768


def _entry(id, *, out, window=None, reasoning=True, pay=True):
    return MM.ModelEntry(id=id, name=id.split(".")[1], provider=id.split(".")[1].split("-")[0], source="managed",
                         tools="native", vision=False, reasoning=reasoning, price_in=out / 4, price_out=out,
                         can_pay=pay, window=window)


HAIKU = _entry("synapse.claude-haiku", out=5, window=200_000)
#: The dearest within the automatic ceiling, so the old step up's pick, and too small for the planner.
SMALL = _entry(ALIAS, out=15, window=SMALL_WINDOW)
SONNET = _entry("synapse.claude-sonnet", out=12, window=200_000)
#: Its window is not known: never ruled out for it, as the engine's own pick never rules one out.
UNKNOWN = _entry("synapse.unknown-strong", out=14)
MENU = [HAIKU, SMALL, SONNET]


@pytest.fixture
def menu(monkeypatch):
    now = {"menu": list(MENU)}

    async def fake():
        return list(now["menu"])

    monkeypatch.setattr(MM, "menu", fake)
    return now


# ── a step up ─────────────────────────────────────────────────────────────────────────────────────────────────────

class TestAStepUpForLongInputsCanReadThem:
    def test_a_model_known_too_small_is_passed_over(self):
        assert MM.stronger_than(HAIKU.id, "simple", has_tools=True, menu=MENU, long_inputs=True).id == SONNET.id

    def test_a_model_whose_window_is_not_known_is_still_a_step_up(self):
        got = MM.stronger_than(HAIKU.id, "simple", has_tools=True, menu=[*MENU, UNKNOWN], long_inputs=True)
        assert got.id == UNKNOWN.id

    def test_without_long_inputs_the_step_up_is_as_before(self):
        assert MM.stronger_than(HAIKU.id, "simple", has_tools=True, menu=MENU).id == SMALL.id


RUN, SESSION = "builder_run_f1591", "builder::panel:f1591"


async def _failed_run():
    from core.native_builder import seed
    from core.orchestration.engine import OrchestrationEngine
    from core.orchestration.state import SharedState

    await seed.seed_native_builder()  # the run goes on with the builder's own definition
    run = OrchestrationRun(run_id=RUN, orchestration_id="orch_native_builder", session_id=SESSION, status="failed",
                           current_step_id="step_plan",
                           shared_state={OrchestrationEngine.MODEL_OVERRIDE_KEY: HAIKU.id})
    await SharedState(run).checkpoint()


class TestSynapseAIIsSteppedUpOnlyToAModelThatCanHoldIt:
    async def test_a_failed_turns_offer(self, menu):
        await _failed_run()
        assert (await try_stronger.panel_offer(RUN))["model"] == SONNET.id

    async def test_its_press(self, menu):
        await _failed_run()
        assert (await try_stronger.panel_retry(RUN, SESSION))["model"] == SONNET.id

    async def test_the_effort_offers_use_stronger(self, menu):
        found = await effort._stronger(HAIKU.id)
        assert found is not None and found[0].id == SONNET.id

    async def test_an_agents_chat_offer_is_as_before(self, menu):
        """A plain agent's chat turn is not the planner: its step up keeps its rule."""
        assert (await try_stronger.offer(HAIKU.id, has_tools=True, retry_of="t1"))["model"] == SMALL.id


class TestThePlansStrongerModelForALongInputAgentCanReadThem:
    PLAN = {"kind": "create", "title": "Contracts", "summary": "Reads contracts.",
            "steps": [{"id": "a", "type": "agent", "name": "Read", "does": "Reads each contract", "agent": "Reader",
                       "next": "end"}],
            "agents": [{"name": "Reader", "purpose": "Reads whole contracts", "long_inputs": True,
                        "stronger_helps": True}]}

    async def test_offered_and_chosen(self, menu, monkeypatch):
        from core.native_builder import build_plan

        monkeypatch.setattr(MM, "workspace_model", lambda: HAIKU.id)
        plan = O.Plan.from_draft(self.PLAN)
        picks, issues, _ = await build_plan.pick_models(plan, [])
        assert issues == [] and picks["Reader"].model == HAIKU.id
        assert picks["Reader"].stronger == SONNET.id, "the card's stronger model can read what this agent reads"
        chosen, _, _ = await build_plan.pick_models(plan, ["Reader"])
        assert chosen["Reader"].model == SONNET.id


# ── a host's word on a window ─────────────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def host(monkeypatch):
    """The host's windows (as cloud says its platform models'), every ask it was sent, and a switch to fail it."""
    said = {"asks": [], "fail": False, "table": {ALIAS: SMALL_WINDOW}}

    async def windows_of(ids):
        said["asks"].append(list(ids))
        if said["fail"]:
            raise RuntimeError("catalogue not read")
        return {i: said["table"][i] for i in ids if i in said["table"]}

    setter = getattr(windows, "set_host_windows", None)
    if setter is not None:
        setter(windows_of)
    yield said
    if setter is not None:
        setter(None)


class TestAHostSaysAModelsWindow:
    async def test_the_hosts_word_comes_before_the_table(self, host):
        assert windows.window_tokens(ALIAS) == 128_000, "the table reads the alias as Mistral's own API"
        assert await windows.window_of(ALIAS) == SMALL_WINDOW

    async def test_a_model_the_host_says_nothing_of_reads_the_table(self, host):
        assert await windows.window_of("claude-haiku-4-5") == windows.window_tokens("claude-haiku-4-5")

    async def test_without_a_host_it_is_the_table(self):
        assert await windows.window_of(ALIAS) == windows.window_tokens(ALIAS)


class TestTheHostsWordIsKeptAFewMinutes:
    async def test_a_second_ask_within_the_time_does_not_reach_the_host(self, host):
        assert [await windows.window_of(ALIAS), await windows.window_of(ALIAS)] == [SMALL_WINDOW, SMALL_WINDOW]
        assert host["asks"] == [[ALIAS]]

    async def test_nothing_said_is_kept_too(self, host):
        """At most one ask per model per while, a model the host does not know included (cloud: one SELECT)."""
        await windows.window_of("claude-haiku-4-5")
        await windows.window_of("claude-haiku-4-5")
        assert host["asks"] == [["claude-haiku-4-5"]]

    async def test_a_word_past_its_time_is_asked_again(self, host, monkeypatch):
        now = {"t": 1_000.0}
        monkeypatch.setattr(windows, "_clock", lambda: now["t"])
        await windows.window_of(ALIAS)
        now["t"] += windows.HOST_TTL - 1
        await windows.window_of(ALIAS)
        assert len(host["asks"]) == 1, "still in its time"
        now["t"] += 2
        host["table"][ALIAS] = 16_384  # the catalogue changed
        assert await windows.window_of(ALIAS) == 16_384
        assert len(host["asks"]) == 2

    async def test_a_host_that_failed_is_not_kept(self, host):
        host["fail"] = True
        assert await windows.window_of(ALIAS) == windows.window_tokens(ALIAS), "the table, for this ask"
        host["fail"] = False
        assert await windows.window_of(ALIAS) == SMALL_WINDOW, "asked again, not the failure kept"
        assert len(host["asks"]) == 2

    async def test_a_new_host_starts_with_nothing_kept(self, host):
        await windows.window_of(ALIAS)
        host["table"][ALIAS] = 8_000
        windows.set_host_windows(windows._host)
        assert await windows.window_of(ALIAS) == 8_000


# ── a step's budget ───────────────────────────────────────────────────────────────────────────────────────────────

#: Bedrock's own id for the model the alias names: the table reads it as 32,768 (F1586).
BEDROCK_ID = "mistral.mistral-large-2402-v1:0"


def _caps() -> tuple[int, int]:
    """The read cap a step on the alias works within: by the host's 32K (the Bedrock id's, as the table reads it),
    and by the table's reading of the alias (128K)."""
    settings = load_settings()
    by_host, by_table = budget(BEDROCK_ID, settings).read_cap, budget(ALIAS, settings).read_cap
    assert windows.window_tokens(BEDROCK_ID) == SMALL_WINDOW and by_host != by_table, "the test tells the two apart"
    return by_host, by_table


class TestAStepsBudgetIsTheModelsTheHostNames:
    async def test_the_budget(self, host):
        from core.context_budget import step_budget

        got = await step_budget(ALIAS, load_settings())
        assert got.window == SMALL_WINDOW and got.read_cap == _caps()[0]
        assert budget(ALIAS, load_settings()).window == 128_000, "the table alone, as before"

    def test_a_window_the_caller_knows_wins(self):
        assert budget(ALIAS, load_settings(), window=SMALL_WINDOW) == budget(BEDROCK_ID, load_settings())


class _Stop(Exception):
    """Ends the step once its prompt is being built: what this test reads is known by then."""


@pytest.fixture
def prompt_cap(monkeypatch):
    """The read cap in force while each step builds its model's prompt (`step_refs.read_cap_now`)."""
    seen: list = []

    async def build_step_prompt(*_a, **_k):
        seen.append(step_refs.read_cap_now())
        raise _Stop

    def fill(*_a, **_k):
        seen.append(step_refs.read_cap_now())
        raise _Stop

    async def no_agent(*_a, **_k):
        return None

    monkeypatch.setattr(context_mod, "build_step_prompt", build_step_prompt)
    monkeypatch.setattr(context_mod, "_fill_task_template", fill)
    monkeypatch.setattr(resolved_mod, "resolve_for_run", no_agent)
    return seen


async def _execute(kind: str, **step):
    run = OrchestrationRun(run_id="run_f1591", orchestration_id="orch_f1591", shared_state={})
    engine = types.SimpleNamespace(current_transition=None, global_variables={})
    executor = steps_mod.STEP_EXECUTORS[steps_mod.StepType(kind)]
    with pytest.raises(_Stop):
        async for _ in executor.execute(StepConfig(id="s1", name="One step", type=kind, model=ALIAS, **step), run,
                                        engine):
            pass


class TestEachStepBuildsItsPromptWithinItsModelsWindow:
    """Each of `_reading_for`'s three doors, run as a step runs: a saved output is inline in the prompt only up to the
    read share of the model the host names (D339 P4), never the table's reading of its alias."""

    async def test_an_agent_step(self, host, prompt_cap):
        await _execute("agent", agent_id="agent_f1591")
        assert prompt_cap == [_caps()[0]]

    async def test_an_evaluator_step(self, host, prompt_cap):
        await _execute("evaluator", route_map={"yes": None}, evaluator_prompt="Is it done?")
        assert prompt_cap == [_caps()[0]]

    async def test_an_llm_step(self, host, prompt_cap):
        await _execute("llm")
        assert prompt_cap == [_caps()[0]]
