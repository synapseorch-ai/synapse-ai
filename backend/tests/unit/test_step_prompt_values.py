"""A value is a value, never a template — the step prompt's two reference families (F19, F3).

`{state.x}` and `{{ … }}` used to be two passes, and the order was a leak: a state
value written by the first was read by the second as a template, so a crawled
page or a model answer that said `{{ global.STRIPE_KEY }}` reached the next step
— and the run log — with the real value. Now `context._fill_task_template` is
one scan, and nothing it inserts is scanned again. The run's own input is a
value too (D97): with no prompt template the task text is the input as typed,
the rule D95 set for `@[path]`.
"""

from __future__ import annotations

import types

import pytest

from _fakes import seed as S

SECRET = "s3cret-VALUE"


def _server():
    return types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})


async def _run(orch_dict, initial_input="go", *, globals_=None):
    from core.models_orchestration import Orchestration
    from core.orchestration.engine import OrchestrationEngine

    engine = OrchestrationEngine(Orchestration.model_validate(orch_dict), _server())
    if globals_ is not None:
        async def _load():
            return dict(globals_)
        engine._load_global_variables = _load
    events = [ev async for ev in engine.run(initial_input, run_id=f"run_{orch_dict['id']}")]
    return events, engine


@pytest.fixture
def logged(monkeypatch):
    """Every event the engine hands its run logger — `_log_prompt` included,
    which the engine consumes rather than yields."""
    from core.orchestration.logger import OrchestrationLogger

    seen: list[dict] = []
    original = OrchestrationLogger.log_event

    def capture(self, event):
        seen.append(event)
        return original(self, event)

    monkeypatch.setattr(OrchestrationLogger, "log_event", capture)
    return seen


def _prompt(events, step_id):
    [p] = [e for e in events if e.get("type") == "_log_prompt" and e.get("orch_step_id") == step_id]
    return p["prompt"]


def _llm(step_id, template, **extra):
    return {"id": step_id, "name": step_id, "type": "llm", "prompt_template": template,
            "output_key": f"{step_id}_out", "model": "claude-test", "next_step_id": None, **extra}


def _set(step_id, content, key, next_step_id):
    """A print step writes `content` into `shared_state[key]` — it renders
    `{state.x}` and nothing else, so `{{ … }}` arrives in state as written."""
    return {"id": step_id, "name": step_id, "type": "print", "print_content": content,
            "output_key": key, "next_step_id": next_step_id}


def _orch(entry, steps):
    return S.make_orchestration(id=f"orch_v_{entry}", entry_step_id=entry, steps=steps)


async def _chain(fake_llm, logged, note, template):
    """`note` lands in state, then an llm step renders `template` over it."""
    await _run(_orch("p1", [_set("p1", note, "note", "s1"), _llm("s1", template)]),
               globals_={"KEY": SECRET, "OTHER": "other-VALUE"})
    return _prompt(logged, "s1")


# ── a state value is a value ───────────────────────────────────────────────────
class TestAStateValueIsAValue:
    async def test_a_global_reference_inside_a_state_value_stays_as_written(self, fake_llm, logged):
        prompt = await _chain(fake_llm, logged, "see {{ global.KEY }}", "Echo {state.note}")
        assert "see {{ global.KEY }}" in prompt
        assert SECRET not in prompt

    async def test_the_same_through_the_double_brace_form(self, fake_llm, logged):
        prompt = await _chain(fake_llm, logged, "see {{ global.KEY }}", "Echo {{ state.note }}")
        assert "see {{ global.KEY }}" in prompt
        assert SECRET not in prompt

    async def test_the_same_through_the_unspaced_form(self, fake_llm, logged):
        prompt = await _chain(fake_llm, logged, "see {{ global.KEY }}", "Echo {{state.note}}")
        assert "see {{ global.KEY }}" in prompt
        assert SECRET not in prompt

    async def test_a_state_reference_inside_a_state_value_is_not_substituted(self, fake_llm, logged):
        """Seeded through a `state_schema` default — a print step would render it."""
        orch = S.make_orchestration(id="orch_v_default", entry_step_id="s1",
                                    steps=[_llm("s1", "Echo {state.note} and {{ state.note }}")],
                                    state_schema={"note": {"type": "string", "default": "see {state.user_input}"}})
        await _run(orch, initial_input="the input")
        prompt = _prompt(logged, "s1")
        assert prompt.count("see {state.user_input}") == 2 and "see the input" not in prompt

    async def test_the_run_log_carries_the_words_not_the_secret(self, fake_llm, logged):
        """The logged `_log_prompt` IS the transcript's INPUT PROMPT line."""
        prompt = await _chain(fake_llm, logged, "{{ global.KEY }}", "Echo {state.note}")
        assert prompt.count("{{ global.KEY }}") == 1 and SECRET not in prompt


# ── the run's input is a value (D97) ──────────────────────────────────────────
class TestTheRunsInputIsAValue:
    async def test_a_global_reference_in_the_input_stays_as_written(self, fake_llm, logged):
        await _run(_orch("s1", [_llm("s1", "")]), initial_input="please use {{ global.KEY }}",
                   globals_={"KEY": SECRET})
        prompt = _prompt(logged, "s1")
        assert "please use {{ global.KEY }}" in prompt and SECRET not in prompt

    async def test_a_state_reference_in_the_input_stays_as_written(self, fake_llm, logged):
        await _run(_orch("s1", [_llm("s1", "")]), initial_input="echo {state.user_input}")
        assert "echo {state.user_input}" in _prompt(logged, "s1")

    async def test_the_input_is_still_the_task_when_there_is_no_template(self, fake_llm, logged):
        await _run(_orch("s1", [_llm("s1", "")]), initial_input="write a haiku")
        assert "## TASK\nwrite a haiku" in _prompt(logged, "s1")


# ── the template a member typed still renders ─────────────────────────────────
class TestTheTemplateStillRenders:
    async def test_both_families_render_once_each(self, fake_llm, logged):
        prompt = await _chain(fake_llm, logged, "the note",
                              "Key {{ global.KEY }}; note {state.note}; again {{ state.note }}")
        assert f"Key {SECRET}; note the note; again the note" in prompt
        assert prompt.count(SECRET) == 1

    async def test_the_unspaced_form_renders_the_value_not_braces_around_it(self, fake_llm, logged):
        """F3: `{{state.note}}` used to render `{the note}`."""
        prompt = await _chain(fake_llm, logged, "the note", "Echo {{state.note}}")
        assert "Echo the note" in prompt and "{the note}" not in prompt

    async def test_an_unknown_reference_stays_visible(self, fake_llm, logged):
        prompt = await _chain(fake_llm, logged, "the note", "{{ global.missing }} {state.missing}")
        assert "{{ global.missing }} {state.missing}" in prompt

    async def test_a_global_value_is_not_a_template_either(self, fake_llm, logged):
        await _run(_orch("s1", [_llm("s1", "Key {{ global.KEY }}")]),
                   globals_={"KEY": "{{ global.OTHER }} {state.user_input}", "OTHER": "other-VALUE"})
        prompt = _prompt(logged, "s1")
        assert "Key {{ global.OTHER }} {state.user_input}" in prompt and "other-VALUE" not in prompt


# ── the one pass, directly ────────────────────────────────────────────────────
STATE = {"note": "see {{ global.KEY }} and {state.other}", "other": "O", "d": {"a": 1}}
GLOBALS = {"KEY": SECRET, "OTHER": "{state.note}"}


@pytest.mark.parametrize(("template", "expected"), [
    ("{state.note}", "see {{ global.KEY }} and {state.other}"),
    ("{{ state.note }}", "see {{ global.KEY }} and {state.other}"),
    ("{{state.note}}", "see {{ global.KEY }} and {state.other}"),
    ("{{ global.KEY }}", SECRET),
    ("{{ global.OTHER }}", "{state.note}"),
    ("{state.d}", "{'a': 1}"),
    ("{{ state.d }}", '{"a": 1}'),
    ("{state.nope} {{ state.nope }} {{ global.nope }} {{ bare }}",
     "{state.nope} {{ state.nope }} {{ global.nope }} {{ bare }}"),
    ("plain text", "plain text"),
], ids=["legacy", "spaced", "unspaced", "global", "global-holding-state-ref",
        "legacy-dict-is-str", "double-brace-dict-is-json", "unknown-stay", "plain"])
def test_fill_task_template_is_one_pass(template, expected):
    from core.orchestration.context import _fill_task_template

    assert _fill_task_template(template, STATE, GLOBALS) == expected
