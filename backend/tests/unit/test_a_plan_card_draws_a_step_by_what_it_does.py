"""Synapse AI's plan card draws a step by what it does, as the run views do: a Tool step with its inputs set is logic
(F1672, F1656/F1657's rule on the card).

2.1 draws a step on the run screens, the workflow's path and the canvas by what it does (F1656, F1657): a Tool step
whose arguments are set on it runs no model, so it is logic. The plan card still gave each step its type's role
(`roleOf(type)`, tool → "calls a model"), so the same step read "calls a model" on the card that asked to build it and
logic once built. The card's steps come from the checked plan's object, whose `model_fills` lists the Tool steps a model
fills in (F967); a Tool step not among them has its inputs set. Now the card reads each step with `stepRole`, as the
run views do.

Near misses: an LLM step calls a model; a Tool step a model fills in calls a model; a plan object from before
`model_fills` reads by its type, as it did; a Human step is a person's.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "frontend/src"
PLAN = SRC / "components/assistant/plan.ts"

#: Node resolves the app's `@/` imports to `frontend/src`, and a relative import without its extension to the `.ts`
#: beside it, as the bundler does.
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
def roles(tmp_path_factory):
    exe = shutil.which("node")
    if exe is None:
        pytest.skip("node is not on PATH: the plan card's reading is TypeScript")
    where = tmp_path_factory.mktemp("node-hooks")
    (where / "hooks.mjs").write_text(HOOKS % json.dumps(str(SRC)))
    (where / "register.mjs").write_text(
        "import { register } from 'node:module';\n"
        f"register({json.dumps((where / 'hooks.mjs').as_uri())});\n")

    def card(steps: list[dict], **plan) -> dict:
        view = {"object": "plan", "outcome": "ready", "offers": [],
                "plan": {"kind": "create", "title": "Weather note", "steps": steps, **plan}}
        program = (f"import {{ planFromPlanObject }} from {json.dumps(PLAN.as_uri())};\n"
                   f"const p = planFromPlanObject({json.dumps(view)}, 'Make it.');\n"
                   "console.log(JSON.stringify(Object.fromEntries(p.steps.map((s) => [s.name, s.role]))));")
        done = subprocess.run([exe, "--experimental-strip-types", "--no-warnings", "--import",
                               (where / "register.mjs").as_uri(), "--input-type=module", "-e", program],
                              capture_output=True, text=True, timeout=60)
        assert done.returncode == 0, done.stderr[-3000:]
        return json.loads(done.stdout)
    return card


FETCH = {"id": "fetch", "type": "tool", "name": "Fetch the forecast", "tool": "http_get"}
WRITE = {"id": "write", "type": "llm", "name": "Write the note"}
ASK = {"id": "ask", "type": "human", "name": "Ask me first"}


def test_a_tool_step_with_its_inputs_set_is_logic_on_the_card(roles):
    assert roles([FETCH, WRITE], model_fills=[])["Fetch the forecast"] == "logic"


def test_an_llm_step_calls_a_model(roles):
    assert roles([FETCH, WRITE], model_fills=[])["Write the note"] == "model"


def test_a_tool_step_a_model_fills_in_calls_a_model(roles):
    assert roles([FETCH, WRITE], model_fills=["fetch"])["Fetch the forecast"] == "model"


def test_a_plan_object_from_before_model_fills_reads_by_its_type(roles):
    assert roles([FETCH, WRITE])["Fetch the forecast"] == "model"


def test_a_human_step_is_a_persons(roles):
    assert roles([ASK, FETCH], model_fills=[])["Ask me first"] == "person"
