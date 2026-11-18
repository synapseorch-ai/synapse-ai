"""A run draws a step as "Called a model" only when it called one — F1656.

The run screens colour each step by who did its work (D211): cyan "Called a model", grey "Logic", amber "A person".
They read it from the step's type alone, and a Tool step was always a model's ("a Tool step asks a model for the
call's arguments"). But the engine asks no model when the arguments are set on the step (`tool_arguments`, E1): a
run of a Tool step with set arguments and a Transform called no model, said "0 tokens, $0 model cost", and still drew
its Tool step cyan, as having called one.

A step that ran is drawn by what it did, from the spend the run's record carries (`step_usage` / `entry_usage`, both
products, CE1): tokens spent → a model; none → logic. Some steps are never judged by spend:
  - a Human step is a person's;
  - a Parallel, a Loop or a Workflow step is logic: the calls inside its window are its steps' (a Workflow step's
    entry is swept its child run's calls for cost, F284), and those steps show their own (E3);
  - a Retrieve step keeps its type: its query's embedding is never logged as a call, so no spend is no evidence.
A step with no finished record of its own (not reached, running, waiting, failed, cancelled), or a run whose record
carries no spend at all, is drawn by what its config does: its type, except that a Tool step with set arguments is
logic, as the engine runs it. A run list's path knows no spend, so it is drawn the same way.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "frontend/src"
MODEL = SRC / "components/run/model.ts"

#: Node resolves the app's `@/` imports to `frontend/src`, and a relative import without its extension to the `.ts`
#: beside it, as the bundler does: what the module imports at run time is what is tested.
HOOKS = r"""
import { existsSync } from 'node:fs';
import { dirname, resolve as join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
const SRC = %s;
export async function resolve(specifier, context, next) {
  let base = null;
  if (specifier.startsWith('@/')) base = join(SRC, specifier.slice(2));
  else if (specifier.startsWith('.') && context.parentURL && !/\.[cm]?[jt]sx?$/.test(specifier))
    base = join(dirname(fileURLToPath(context.parentURL)), specifier);
  if (base) for (const ext of ['.ts', '.tsx', '/index.ts']) {
    if (existsSync(base + ext)) return next(pathToFileURL(base + ext).href, context);
  }
  return next(specifier, context);
}
"""


@pytest.fixture(scope="module")
def node(tmp_path_factory):
    exe = shutil.which("node")
    if exe is None:
        pytest.skip("node is not on PATH: the run screens' model is TypeScript")
    where = tmp_path_factory.mktemp("node-hooks")
    (where / "hooks.mjs").write_text(HOOKS % json.dumps(str(SRC)))
    (where / "register.mjs").write_text(
        "import { register } from 'node:module';\n"
        f"register({json.dumps((where / 'hooks.mjs').as_uri())});\n")

    def run(script: str):
        program = (f"import * as M from {json.dumps(MODEL.as_uri())};\n"
                   f"console.log(JSON.stringify(await (async () => {{ {script} }})()));")
        done = subprocess.run([exe, "--experimental-strip-types", "--no-warnings", "--import",
                               (where / "register.mjs").as_uri(), "--input-type=module", "-e", program],
                              capture_output=True, text=True, timeout=60)
        assert done.returncode == 0, done.stderr[-3000:]
        return json.loads(done.stdout)
    return run


# ── a made-up workflow and run ───────────────────────────────────────────────

T0 = "2026-01-01T09:00:00Z"
NOW = "2026-01-01T09:05:00Z"


def at(seconds: int) -> str:
    return f"2026-01-01T09:00:{seconds:02d}Z"


def step(id: str, type: str, nxt: str | None = "end", **more) -> dict:
    return {"id": id, "name": id.replace("_", " ").title(), "type": type, "next_step_id": nxt, **more}


def workflow(*steps: dict) -> dict:
    return {"id": "orch_harbor", "name": "Harbor & Pine forecast", "entry_step_id": steps[0]["id"],
            "steps": [*steps, {"id": "end", "name": "End", "type": "end"}]}


def entry(id: str, type: str, start: int, status: str = "completed") -> dict:
    out = {"step_id": id, "step_name": id.replace("_", " ").title(), "step_type": type, "status": status,
           "started_at": at(start)}
    if status not in ("running", "paused"):
        out.update(ended_at=at(start + 1), duration_seconds=1)
    return out


def spent(tokens: int, *, models=("openai.gpt-4.1-mini",), tools=()) -> dict:
    return {"turns": 1 if tokens else 0, "models": list(models), "tools": list(tools), "tokens": tokens,
            "input_tokens": tokens, "output_tokens": 0, "cost_usd": 0.0001 * tokens, "context_peak_chars": 0,
            "latency_seconds": 0.4}


def run(history: list[dict], *, status: str = "completed", current: str | None = None, **spend) -> dict:
    return {"id": "run_harbor", "status": status, "waitingForHuman": status == "paused", "startedAt": T0,
            "endedAt": None if status in ("running", "paused") else at(30), "currentStepId": current,
            "history": history, **spend}


def timeline(node, r: dict, wf: dict) -> dict:
    return dict(node(f"return M.timelineRows({json.dumps(r)}, {json.dumps(wf)}, [], Date.parse({json.dumps(NOW)}))"
                     ".rows.map((row) => [row.key === 'current' ? 'now:' + row.stepId : row.stepId, row.role]);"))


def stepper(node, r: dict, wf: dict) -> dict:
    return dict(node(f"return M.stepperRows({json.dumps(r)}, {json.dumps(wf)}).map((row) => [row.stepId, row.role]);"))


def glyph(node, r: dict, wf: dict) -> dict:
    return dict(node(f"return M.glyphOf({json.dumps(wf)}, {json.dumps(r)}).map((bar) => [bar.id, bar.role]);"))


SET_ARGS = {"tool_name": "http_get", "tool_arguments": {"url": "https://example.com/forecast?city={{city}}"}}


# ── the run that called no model ─────────────────────────────────────────────

def test_a_tool_step_with_set_arguments_and_a_transform_draw_no_model_call(node):
    """The run said "0 tokens, $0 model cost": its record has no call at all (`{}` and `[]`, as both products send)."""
    wf = workflow(step("fetch_forecast", "tool", "shape_answer", **SET_ARGS), step("shape_answer", "transform"))
    r = run([entry("fetch_forecast", "tool", 0), entry("shape_answer", "transform", 2)], stepUsage={}, entryUsage=[])
    assert timeline(node, r, wf) == {"fetch_forecast": "logic", "shape_answer": "logic"}, "The path"
    assert stepper(node, r, wf) == {"fetch_forecast": "logic", "shape_answer": "logic"}, "the stepper"
    assert glyph(node, r, wf) == {"fetch_forecast": "logic", "shape_answer": "logic"}, "the run list's path"


def test_a_tool_step_that_asked_a_model_for_its_arguments_did_call_one(node):
    wf = workflow(step("look_up", "tool", tool_name="http_get"))
    r = run([entry("look_up", "tool", 0)], stepUsage={"look_up": spent(240)}, entryUsage=[spent(240)])
    assert timeline(node, r, wf) == {"look_up": "model"}
    assert stepper(node, r, wf) == {"look_up": "model"}
    assert glyph(node, r, wf) == {"look_up": "model"}, "a list knows no spend: its config asks a model"


# ── near misses: what stays as it was ────────────────────────────────────────

def test_an_llm_step_and_an_agent_with_tools_that_spent_tokens_called_a_model(node):
    wf = workflow(step("draft", "llm", "research"), step("research", "agent"))
    r = run([entry("draft", "llm", 0), entry("research", "agent", 2)],
            stepUsage={"draft": spent(120), "research": spent(900, tools=("web_read",))},
            entryUsage=[spent(120), spent(900, tools=("web_read",))])
    assert timeline(node, r, wf) == {"draft": "model", "research": "model"}
    assert stepper(node, r, wf) == {"draft": "model", "research": "model"}


def test_a_human_step_is_a_persons_whatever_its_window_holds(node):
    wf = workflow(step("approve", "human", "write"), step("write", "llm"))
    waiting = run([entry("approve", "human", 0, status="paused")], status="paused", current="approve",
                  stepUsage={}, entryUsage=[None])
    assert timeline(node, waiting, wf)["approve"] == "person"
    assert stepper(node, waiting, wf) == {"approve": "person", "write": "model"}, "not reached: by its config"
    answered = run([entry("approve", "human", 0), entry("write", "llm", 2)], stepUsage={"write": spent(80)},
                   entryUsage=[spent(80), spent(80)])
    assert timeline(node, answered, wf) == {"approve": "person", "write": "model"}


def test_a_container_or_a_workflow_step_stays_logic_though_its_window_holds_its_steps_calls(node):
    wf = workflow(step("each_city", "loop", "run_report", loop_step_ids=["summarise"]),
                  step("summarise", "llm", None), step("run_report", "workflow"))
    r = run([entry("summarise", "llm", 0), entry("each_city", "loop", 0), entry("run_report", "workflow", 3)],
            stepUsage={"summarise": spent(50), "each_city": spent(10), "run_report": spent(400)},
            entryUsage=[spent(50), spent(10), spent(400)])
    assert timeline(node, r, wf) == {"summarise": "model", "each_city": "logic", "run_report": "logic"}


def test_a_retrieve_step_keeps_its_type_since_an_embedding_is_never_logged_as_a_call(node):
    wf = workflow(step("find_notes", "retrieve"))
    r = run([entry("find_notes", "retrieve", 0)], stepUsage={}, entryUsage=[])
    assert timeline(node, r, wf) == {"find_notes": "model"}


def test_a_browser_step_whose_changed_page_called_its_agent_called_a_model(node):
    wf = workflow(step("replay_login", "browser", "file_it"), step("file_it", "browser"))
    r = run([entry("replay_login", "browser", 0), entry("file_it", "browser", 2)],
            stepUsage={"replay_login": spent(700)}, entryUsage=[spent(700), None])
    assert timeline(node, r, wf) == {"replay_login": "model", "file_it": "logic"}


def test_a_step_with_no_finished_record_is_drawn_by_what_its_config_does(node):
    wf = workflow(step("ask_model", "llm", "fetch_set"), step("fetch_set", "tool", "fetch_chosen", **SET_ARGS),
                  step("fetch_chosen", "tool", tool_name="http_get"))
    failed = run([entry("ask_model", "llm", 0, status="failed")], status="failed", current="ask_model",
                 stepUsage={}, entryUsage=[])
    assert stepper(node, failed, wf) == {"ask_model": "model", "fetch_set": "logic", "fetch_chosen": "model"}, \
        "a failed LLM step that got no answer still asked a model; the steps not reached by their config"
    tool_failed = run([entry("ask_model", "llm", 0), entry("fetch_set", "tool", 2, status="failed")],
                      status="failed", current="fetch_set", stepUsage={"ask_model": spent(60)},
                      entryUsage=[spent(60), None])
    assert stepper(node, tool_failed, wf)["fetch_set"] == "logic"
    running = run([entry("ask_model", "llm", 0)], status="running", current="fetch_set",
                  stepUsage={"ask_model": spent(60)}, entryUsage=[spent(60)])
    assert timeline(node, running, wf) == {"ask_model": "model", "now:fetch_set": "logic"}


def test_a_record_that_carries_no_spend_draws_each_step_by_its_config(node):
    """A product whose record says nothing of spend: no spend is not "called nothing"."""
    wf = workflow(step("draft", "llm", "fetch_set"), step("fetch_set", "tool", **SET_ARGS))
    r = run([entry("draft", "llm", 0), entry("fetch_set", "tool", 2)])
    assert timeline(node, r, wf) == {"draft": "model", "fetch_set": "logic"}


def test_a_record_with_spend_by_step_only_is_read_by_step(node):
    wf = workflow(step("draft", "llm", "shape"), step("shape", "transform"))
    r = run([entry("draft", "llm", 0), entry("shape", "transform", 2)], stepUsage={"draft": spent(30)})
    assert timeline(node, r, wf) == {"draft": "model", "shape": "logic"}


# ── the screens draw the role the model gives ────────────────────────────────

def test_the_stepper_grounds_each_icon_in_its_rows_role():
    stepper_src = (SRC / "components/run/RunStepper.tsx").read_text("utf-8")
    assert "<StepIcon type={row.type} role={row.role}" in stepper_src, "the stepper's icon takes the row's role"
    icon = (SRC / "components/run/StepIcon.tsx").read_text("utf-8")
    assert "role?: StepRole" in icon and "GROUND[role ?? roleOf(type)]" in icon, \
        "StepIcon grounds a given role, else its type's (the canvas, the palette)"
    timeline_src = (SRC / "components/run/RunTimeline.tsx").read_text("utf-8")
    assert "BAR[row.role]" in timeline_src and "roles.has(role)" in timeline_src, "The path and its legend"
