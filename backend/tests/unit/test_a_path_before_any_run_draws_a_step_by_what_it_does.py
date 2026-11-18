"""A workflow's path, before any run, draws each step by what its config does (F1657).

A run's screens draw a step by what it did (F1656). Before any run, the workflow list's path, an example's path and
the canvas drew each step by its type alone, so a Tool step whose arguments are set on it, which the engine runs with
no model (E1), read as "calling a model": a tile in the machine colour, and a label counting it among the steps that
call a model. Now they draw it by its config (`run/model.ts` `stepRole`): a Tool step with set arguments is logic.

`templatePath` gives each step's role beside its type, and `pathLabel` counts by them; the path's tiles, the
workflow list's row, an example's card, the canvas's step (its icon and its glow as it works), the canvases' minimaps
and a step's result card use the roles. The palette stays as
it is: an entry there is a type with no config yet, and a new Tool step asks a model.

Near misses: a Tool step that asks a model for its arguments, an LLM step and a Human step keep their roles.
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
        pytest.skip("node is not on PATH: the path's model is TypeScript")
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


def _workflow(*steps: dict) -> dict:
    chained = [{**s, "next_step_id": nxt["id"] if nxt else None} for s, nxt in zip(steps, [*steps[1:], None])]
    return {"entry_step_id": steps[0]["id"], "steps": chained}


SET = {"id": "fetch", "type": "tool", "tool_arguments": {"url": "https://example.com/forecast"}}
ASKS = {"id": "look_up", "type": "tool"}
SHAPE = {"id": "shape", "type": "transform"}
DRAFT = {"id": "draft", "type": "llm"}
ASK = {"id": "approve", "type": "human"}


def test_a_tool_step_with_set_arguments_is_logic_on_the_path(node):
    got = node(f"return M.templatePath({json.dumps(_workflow(SET, SHAPE))});")
    assert got["types"] == ["tool", "transform"] and got["roles"] == ["logic", "logic"], got
    assert node(f"const p = M.templatePath({json.dumps(_workflow(SET, SHAPE))}); return M.pathLabel(p.types, p.roles);") \
        == "2 steps, 2 logic"


def test_the_other_steps_keep_their_roles(node):
    got = node(f"return M.templatePath({json.dumps(_workflow(ASKS, DRAFT, ASK, SET))});")
    assert got["roles"] == ["model", "model", "person", "logic"] and got["hasPerson"], got
    label = node(f"const p = M.templatePath({json.dumps(_workflow(ASKS, DRAFT, ASK, SET))}); "
                 "return M.pathLabel(p.types, p.roles);")
    assert label == "4 steps, 2 calling a model, 1 for a person, 1 logic", label


def test_the_screens_draw_the_roles():
    path = (SRC / "components/run/StepPath.tsx").read_text("utf-8")
    assert "roles?.[" in path and "pathLabel(types, roles)" in path, "the tiles and the label take the roles"
    assert "roles={row.roles}" in (SRC / "components/run/WorkflowList.tsx").read_text("utf-8")
    assert "roles: StepRole[]" in (SRC / "components/run/workflowRows.ts").read_text("utf-8")
    assert "roles={roles}" in (SRC / "components/settings/import-export/ExamplesView.tsx").read_text("utf-8")
    node_src = (SRC / "components/orchestration/StepNode.tsx").read_text("utf-8")
    assert "role={stepRole(step.type, step)}" in node_src, "the canvas's step"
    assert "const role = stepRole(step.type, step);" in node_src, "the canvas's step glows by it as it works"
    for canvas in ("WorkflowCanvas.tsx", "CanvasWorkbench.tsx"):
        minimap = (SRC / "components/orchestration" / canvas).read_text("utf-8")
        assert "ROLE_FILL[stepRole(node.data?.step?.type, node.data?.step)]" in minimap, f"{canvas}'s minimap"
    card = (SRC / "components/orchestration/workbench/StepResultCard.tsx").read_text("utf-8")
    assert "role={stepRole(step.type, step)}" in card, "a step's result card"
