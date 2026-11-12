"""Every field a step fills in from the run arrives filled, on every step type; a model never reads a placeholder
(F1536, BLD2; Naveen 11-11).

Naveen's "Resolve One Jira Issue" (run_orch_t97n17n_1794384120053_10ec64, OSS): a Human step, "Approve the Plan",
wrote the reply "proceed" to `approve_plan`, and the Evaluator after it, "Plan Approved?", was told "The person was
shown a fix plan and replied: {{ state.approve_plan }}". The Evaluator never rendered its prompt (since the first
commit; F1252 even rewrites an older `{state.x}` there into `{{ state.x }}`), so the model read the reply as an
unfilled placeholder, which is no reply, routed `revise` and looped back to "Plan the Fix". The value was also in its
CONTEXT, as `[approve_plan]`, which nothing tied to the placeholder.

Now an Evaluator fills in its prompt and its route descriptions as an LLM step fills in its prompt, through the one
path a model's prompt takes (`context._fill_task_template`). Naveen's picks:
- A key a model step reads that is not there is said in words, never passed on as the literal: a wrong field in a key
  that is set, or an unknown `{{ global.x }}`, on every model step (Agent, LLM, Tool, Workflow, Evaluator); a key no
  step has written yet stays empty on the steps that write (U74), and is words in an Evaluator, which judges on it.
- A Human or a Print step's text, which a person reads, kept the gap (until F1556, Naveen 11-12: it says it in
  words, as an Evaluator's prompt does); a value typed into a site or searched for (Browser, Retrieve) is as before.
- A bare `{{ x }}` is left as written everywhere: a prompt may show a model a template.

`FIELDS` is every step type's row: what it fills in. A new step type, a new template field in `core.templating`, or a
row with no case here fails this file.
"""
from __future__ import annotations

import json
import re
import types

import pytest

from _fakes import seed as S

#: The fields each step type fills in from the run; `retrieve.query` is the `query` inside the step's `retrieve`.
FIELDS: dict[str, tuple[str, ...]] = {
    "agent": ("prompt_template",),
    "llm": ("prompt_template",),
    "tool": ("prompt_template", "tool_arguments"),
    "workflow": ("prompt_template",),
    "evaluator": ("evaluator_prompt", "route_descriptions"),
    "human": ("human_prompt",),
    "print": ("print_content",),
    "retrieve": ("retrieve.query",),
    "browser": ("recording_inputs",),
    "parallel": (), "merge": (), "end": (), "extract_json": (),
    "loop": (),          # its list, while and stop-early fields are expressions (`core.expressions`), never templates
    "if_else": (),       # the same
    "switch": (),        # the same
    "transform": (),     # code, which reads `state.key`
    "ingest": (),        # a state key's name and its records' field names
}
#: What `core.templating` writes in the one form but no step fills in, and why.
NOT_FILLED = {
    "retrieve.filters": "a metadata filter: JSON values, not offered as a template",
    "ingest": "a state key's name and field names, not text",
}
#: The steps whose text a model reads.
MODEL_STEPS = ("agent", "llm", "tool", "workflow", "evaluator")

STATE_SCHEMA = {
    "approve_plan": {"type": "str", "default": "proceed"},
    "review": {"type": "dict", "default": {"ok": "looks right"}},
}
TEMPLATE = "Reply: {{ state.approve_plan }}; review: {{ state.review.ok }}"
FILLED = "Reply: proceed; review: looks right"
#: What a model reads for a key that is not there (Naveen's pick 1, 11-11).
WRONG_FIELD = "(no value: state.review has no 'nope')"
NO_VARIABLE = "(no value: no variable named 'nope')"
NOT_YET = "(no value yet: no step has written state.never)"
PLACEHOLDER = re.compile(r"\{\{\s*(state|global)\.")
ROUTE = json.dumps({"tool": "route_approved", "arguments": {"reasoning": "the person said go"}})


def _server():
    return types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})


@pytest.fixture
def logged(monkeypatch):
    """Every event the engine hands its run logger, `_log_prompt` included (the engine consumes it)."""
    from core.orchestration.logger import OrchestrationLogger

    seen: list[dict] = []
    original = OrchestrationLogger.log_event

    def capture(self, event):
        seen.append(event)
        return original(self, event)

    monkeypatch.setattr(OrchestrationLogger, "log_event", capture)
    return seen


async def _run(step: dict, *, globals_=None) -> list[dict]:
    from core.models_orchestration import Orchestration
    from core.orchestration.engine import OrchestrationEngine

    orch = S.make_orchestration(id=f"orch_f1536_{step['type']}", entry_step_id="s", state_schema=STATE_SCHEMA,
                                steps=[{"id": "s", "name": "S", "output_key": "out", "next_step_id": None, **step}])
    engine = OrchestrationEngine(Orchestration.model_validate(orch), _server())

    async def _load():
        return dict(globals_ or {})
    engine._load_global_variables = _load
    return [ev async for ev in engine.run("go", run_id=f"run_f1536_{step['type']}")]


def _sent(fake_llm) -> str:
    return "\n".join(str(c.get("prompt_msg") or c.get("_args") or "") for c in fake_llm.calls)


def _logged_prompt(logged) -> str:
    return "\n".join(str(e.get("prompt") or "") for e in logged
                     if e.get("type") == "_log_prompt" and e.get("orch_step_id") == "s")


# ── each step type's own path: what it sends, shows or keeps, with `template` in every field its row names ──────

async def _agent(template, fake_llm, logged, monkeypatch):
    await S.seed_agents([S.make_agent(id="agent_f1536", tools=[], model="claude-haiku-4-5")])
    await _run({"type": "agent", "agent_id": "agent_f1536", "prompt_template": template})
    return _sent(fake_llm)


async def _llm(template, fake_llm, logged, monkeypatch):
    await _run({"type": "llm", "model": "claude-test", "prompt_template": template})
    return _sent(fake_llm)


async def _tool(template, fake_llm, logged, monkeypatch):
    """Its prompt (a model fills the arguments in) as it logs it before it looks for the tool; its set arguments as
    the tool would be called with them. A set argument whose key is not there fails the step, named (E1): kept."""
    from core.orchestration.steps import MissingStateRef, render_tool_arguments

    await _run({"type": "tool", "forced_tool": "no_such_tool_f1536", "prompt_template": template})
    state = {"approve_plan": "proceed", "review": {"ok": "looks right"}}
    try:
        args = json.dumps(render_tool_arguments({"text": template}, state))
    except MissingStateRef as exc:
        args = f"(the step fails: {exc} is not there)"
    return _logged_prompt(logged) + "\n" + args


async def _workflow(template, fake_llm, logged, monkeypatch):
    """Its rendered template is the child's input, logged as its prompt before the child runs."""
    await _run({"type": "workflow", "workflow_id": "orch_gone_f1536", "prompt_template": template})
    return _logged_prompt(logged)


async def _evaluator(template, fake_llm, logged, monkeypatch):
    fake_llm.script([ROUTE])
    await _run({"type": "evaluator", "model": "claude-test", "input_keys": ["approve_plan"],
                "evaluator_prompt": template, "route_map": {"approved": None, "revise": None},
                "route_descriptions": {"approved": f"Pick when: {template}"}})
    return _sent(fake_llm)


async def _human(template, fake_llm, logged, monkeypatch):
    events = await _run({"type": "human", "human_prompt": template})
    return "\n".join(str(e.get("prompt")) for e in events if e.get("type") == "human_input_required")


async def _print(template, fake_llm, logged, monkeypatch):
    events = await _run({"type": "print", "print_content": template})
    return "\n".join(str(e.get("response")) for e in events if e.get("type") == "final")


async def _retrieve(template, fake_llm, logged, monkeypatch):
    from core.knowledge import workflow_steps

    asked: list[str] = []

    async def retrieve(config, query, **_):
        asked.append(query)
        raise workflow_steps.StepFailed("stop here")

    monkeypatch.setattr(workflow_steps, "check_enabled", lambda *a, **k: None)
    monkeypatch.setattr(workflow_steps, "retrieve_config",
                        lambda body, **_: types.SimpleNamespace(**body))
    monkeypatch.setattr(workflow_steps, "retrieve", retrieve)
    await _run({"type": "retrieve", "retrieve": {"kb_ids": ["kb_f1536"], "query": template}})
    return "\n".join(asked)


async def _browser(template, fake_llm, logged, monkeypatch):
    """The recording's inputs as the replay is handed them."""
    from core.browser import client as browser_client
    from core.browser import picks
    from core.browser import recordings as recs

    handed: list[dict] = []

    async def get(_rid):
        return {"name": "Order", "steps": []}

    async def settings_for(*_a, **_k):
        return {}

    def replay_for_step(_key, _doc, _steps, inputs, **_):
        handed.append(inputs)
        raise RuntimeError("stop here")

    monkeypatch.setattr(recs, "get", get)
    monkeypatch.setattr(recs, "input_names", lambda doc: ["order"])
    monkeypatch.setattr(picks, "settings_for", settings_for)
    monkeypatch.setattr(browser_client.tools, "replay_for_step", replay_for_step)
    await _run({"type": "browser", "recording_id": "rec_f1536", "recording_inputs": {"order": template}})
    return "\n".join(json.dumps(i) for i in handed)


CASES = {"agent": _agent, "llm": _llm, "tool": _tool, "workflow": _workflow, "evaluator": _evaluator,
         "human": _human, "print": _print, "retrieve": _retrieve, "browser": _browser}


# ── the guard ─────────────────────────────────────────────────────────────────────────────────────────────────────

class TestEveryStepTypeSaysWhatItFillsIn:
    def test_every_step_type_has_a_row(self):
        from core.models_orchestration import StepType
        from core.orchestration.steps import STEP_EXECUTORS

        types_ = {t.value for t in StepType}
        assert {t.value for t in STEP_EXECUTORS} == types_
        assert set(FIELDS) == types_, "a new step type says here what it fills in from the run, if anything"

    def test_every_row_that_fills_in_has_a_case(self):
        assert {t for t, fields in FIELDS.items() if fields} == set(CASES)

    def test_every_template_field_is_filled_in_or_said_why_not(self):
        """`core.templating` writes these in the one form (F1252). F1536 was one of them read by nothing."""
        from core.templating import TEMPLATE_FIELDS, TEMPLATE_MAPPINGS

        filled = {f.split(".")[0] for fields in FIELDS.values() for f in fields}
        said = {f.split(".")[0] for f in NOT_FILLED}
        assert set(TEMPLATE_FIELDS) | set(TEMPLATE_MAPPINGS) <= filled | said

    def test_every_field_a_row_names_is_a_step_setting(self):
        from core.models_orchestration import StepConfig

        for fields in FIELDS.values():
            for f in fields:
                assert f.split(".")[0] in StepConfig.model_fields, f


@pytest.mark.parametrize("step_type", sorted(CASES))
async def test_a_placeholder_arrives_filled(step_type, fake_llm, logged, monkeypatch):
    got = await CASES[step_type](TEMPLATE, fake_llm, logged, monkeypatch)
    assert FILLED in got, got[-1500:]
    assert not PLACEHOLDER.search(got), got[-1500:]


@pytest.mark.parametrize("step_type", MODEL_STEPS)
async def test_a_model_never_reads_a_placeholder_for_what_is_not_there(step_type, fake_llm, logged, monkeypatch):
    """A wrong field in a key that is set, or an unknown variable, is said in words on every model step."""
    got = await CASES[step_type]("A {{ state.review.nope }} B {{ global.nope }} C", fake_llm, logged, monkeypatch)
    assert f"A {WRONG_FIELD} B {NO_VARIABLE} C" in got, got[-1500:]
    assert not PLACEHOLDER.search(got), got[-1500:]


async def test_an_evaluator_says_a_key_no_step_has_written(fake_llm, logged, monkeypatch):
    """An Evaluator judges on what it reads: a key not written yet is said, not left out."""
    got = await _evaluator("Reply: {{ state.never }}", fake_llm, logged, monkeypatch)
    assert f"Reply: {NOT_YET}" in got
    assert not PLACEHOLDER.search(got)


@pytest.mark.parametrize("step_type", ("agent", "llm"))
async def test_a_writer_still_reads_a_key_no_step_has_written_as_empty(step_type, fake_llm, logged, monkeypatch):
    """U74 stands for the steps that write: on a feedback loop's first pass a writer echoed what it read into its
    draft."""
    got = await CASES[step_type]("Improve it. [{{ state.never }}]", fake_llm, logged, monkeypatch)
    assert "Improve it. []" in got
    assert "no value" not in got


@pytest.mark.parametrize("step_type", ("human", "print"))
async def test_a_person_reads_the_gap_in_words(step_type, fake_llm, logged, monkeypatch):
    """A person's text says the gap in an Evaluator's words, never braces (F1556, Naveen 11-12: it kept the gap)."""
    got = await CASES[step_type]("Reply: {{ state.never }}", fake_llm, logged, monkeypatch)
    assert "Reply: (no value yet: no step has written state.never)" in got
    assert "{{ state.never }}" not in got


async def test_a_bare_name_is_left_as_written(fake_llm, logged, monkeypatch):
    """Naveen's pick 3: a prompt may show a model a template."""
    got = await _evaluator("Write it as {{ approve_plan }}.", fake_llm, logged, monkeypatch)
    assert "Write it as {{ approve_plan }}." in got


# ── the report, end to end: a Human step's answer read by the Evaluator after it ──────────────────────────────────

class TestTheJiraRun:
    """The two steps from run_orch_t97n17n_1794384120053_10ec64, with a judge that routes as a model would: the
    person's reply, when the instructions carry it, else no reply, which is `revise`."""

    def _orch(self):
        return S.make_orchestration(
            id="orch_f1536_jira", entry_step_id="approve", steps=[
                {"id": "approve", "name": "Approve the Plan", "type": "human",
                 "human_prompt": "Here is the fix plan. Reply 'proceed' to go ahead, or say what to change.",
                 "output_key": "approve_plan", "input_keys": [], "next_step_id": "judge"},
                {"id": "judge", "name": "Plan Approved?", "type": "evaluator", "model": "claude-test",
                 "input_keys": ["approve_plan"],
                 "evaluator_prompt": "The person was shown a fix plan and replied: {{ state.approve_plan }}",
                 "route_map": {"approved": "go", "revise": "replan"},
                 "route_descriptions": {"approved": "They agreed to the plan",
                                        "revise": "They asked for changes, or gave no reply"},
                 "output_key": "plan_decision"},
                {"id": "go", "name": "Fix", "type": "print", "print_content": "fixing", "output_key": "done",
                 "next_step_id": None},
                {"id": "replan", "name": "Plan the Fix", "type": "print", "print_content": "replanning",
                 "output_key": "done", "next_step_id": None},
            ])

    @pytest.mark.parametrize("answer", [{"response": "proceed"}, "proceed"], ids=["chat-mapping", "modal-text"])
    async def test_the_reply_reaches_the_evaluator_and_routes_it(self, answer, monkeypatch):
        import core.llm_providers as llm
        from core.models_orchestration import Orchestration
        from core.orchestration.engine import OrchestrationEngine

        prompts: list[str] = []

        async def judge(*_a, prompt_msg="", **_k):
            prompts.append(prompt_msg)
            instructions = prompt_msg.split("CONTEXT:")[0]
            said = re.search(r"replied: (.*)", instructions)
            label = "approved" if said and said.group(1).strip().lower() == "proceed" else "revise"
            return json.dumps({"tool": f"route_{label}", "arguments": {"reasoning": "read the reply"}})

        monkeypatch.setattr(llm, "generate_response", judge)
        orch = self._orch()
        await S.seed_orchestrations([orch])
        run_id = f"run_f1536_jira_{'map' if isinstance(answer, dict) else 'text'}"
        paused = [e async for e in OrchestrationEngine(Orchestration.model_validate(orch), _server())
                  .run("fix JIRA-1", run_id=run_id)]
        assert any(e.get("type") == "human_input_required" for e in paused)

        resumed = [e async for e in OrchestrationEngine.resume(run_id, answer, _server())]

        [prompt] = prompts
        assert "The person was shown a fix plan and replied: proceed" in prompt, prompt
        assert not PLACEHOLDER.search(prompt), prompt
        finals = [e.get("response") for e in resumed if e.get("type") == "final"]
        assert finals[-1] == "fixing", finals
