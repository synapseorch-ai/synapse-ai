"""The editor judges a read by the writers that reach it, as the server does (F1621, part 3).

The editor's own checks (`fieldChecks.ts`, `validate.ts`) said what the server's validator says while every writer of a
key judged every read of it. The server now judges a read by the writers a run can bring to it, and a Parallel's join
has every branch's keys (part 2). The editor follows, from the workflow's entry: a field one reaching writer has and
another lacks, read in a condition or code, is said naming the one that lacks it; a writer that cannot reach the read
no longer switches the field check off; a Loop is judged by the writers that reach it; and a prompt at a Parallel's
join is not told it reads a branch's key empty. On each branch workflow the editor's verdicts are the server's.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from core.builder_tools import _validate_orchestration

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "frontend/src"
CHECKS_TS = SRC / "components/orchestration/fieldChecks.ts"
VALIDATE_TS = SRC / "components/orchestration/validate.ts"

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
def node(tmp_path_factory):
    exe = shutil.which("node")
    if exe is None:
        pytest.skip("node is not on PATH: the editor's checks are TypeScript")
    where = tmp_path_factory.mktemp("node-hooks")
    (where / "hooks.mjs").write_text(HOOKS % json.dumps(str(SRC)))
    (where / "register.mjs").write_text(
        "import { register } from 'node:module';\n"
        f"register({json.dumps((where / 'hooks.mjs').as_uri())});\n")

    def run(module: Path, script: str):
        program = (f"import * as M from {json.dumps(module.as_uri())};\n"
                   f"console.log(JSON.stringify(await (async () => {{ {script} }})()));")
        done = subprocess.run([exe, "--experimental-strip-types", "--no-warnings", "--import",
                               (where / "register.mjs").as_uri(), "--input-type=module", "-e", program],
                              capture_output=True, text=True, timeout=60)
        assert done.returncode == 0, done.stderr[-3000:]
        return json.loads(done.stdout)
    return run


def _editor(node, orch: dict) -> set[tuple]:
    found = node(CHECKS_TS, f"return M.fieldChecks({json.dumps(orch['steps'])}, undefined, "
                            f"{json.dumps(orch['entry_step_id'])});")
    return {(v["stepId"], v["key"], v.get("field"), v["kind"]) for v in found}


def _server(orch: dict) -> set[tuple]:
    """The server's verdicts on `orch`, in the editor's terms."""
    verdict = _validate_orchestration(orch)
    out = set()
    for i in verdict["issues"]:
        if m := re.match(r"Step '(\w+)' reads `(\w+)` from state\.(\w+)", i):
            out.add((m[1], m[3], m[2], "field"))
        if m := re.match(r"Loop '(\w+)' goes through state\.(\w+)(?:\.(\w+))?", i):
            out.add((m[1], m[2], m[3], "list"))
    for w in verdict["warnings"]:
        if "so on a way through" in w and (m := re.match(r"Step '(\w+)' reads `(\w+)` from state\.(\w+)", w)):
            out.add((m[1], m[3], m[2], "way"))
    return out


def _orch(entry: str, *steps: dict) -> dict:
    return {"id": "orch_editor", "name": "Harbor & Pine editor", "entry_step_id": entry, "state_schema": {},
            "steps": [*steps, {"id": "end", "name": "End", "type": "end"}]}


def _if(true: str, false: str) -> dict:
    return {"id": "cond", "name": "Rush?", "type": "if_else", "if_condition": "'rush' in str(state.user_input)",
            "if_true_step_id": true, "if_false_step_id": false}


def _human(sid: str, nxt: str, *fields: str) -> dict:
    return {"id": sid, "name": sid, "type": "human", "human_prompt": "Right?", "output_key": "review",
            "next_step_id": nxt, "human_fields": [{"name": f, "type": "boolean", "label": f"{f}?"} for f in fields]}


DECIDE = {"id": "decide", "name": "Approved?", "type": "if_else", "if_condition": "state.review.approved",
          "if_true_step_id": "end", "if_false_step_id": "end"}


def _model(sid: str, kind: str) -> dict:
    return {"id": sid, "name": sid, "type": "llm", "prompt_template": "List them.", "output_key": "k",
            "next_step_id": "each", "output_fields": [{"name": "items", "type": kind}]}


EACH = {"id": "each", "name": "Each", "type": "loop", "loop_over": "state.k.items", "loop_step_ids": ["one"],
        "next_step_id": "end"}
ONE = {"id": "one", "name": "One", "type": "print", "print_content": "{{ state.loop_item }}", "output_key": "o"}
FIXUP = {"id": "fixup", "name": "Fixup", "type": "transform", "transform_code": "result = {'approved': True}",
         "output_key": "review", "next_step_id": "end"}

CASES = {
    "a field one arm's writer lacks": (
        _orch("cond", _if("asks", "plain"), _human("asks", "decide", "approved"), _human("plain", "decide"), DECIDE),
        {("decide", "review", "approved", "way")}),
    "a writer that cannot reach the read": (
        _orch("ask", _human("ask", "decide"), {**DECIDE, "if_true_step_id": "fixup"}, FIXUP),
        {("decide", "review", "approved", "field")}),
    "a Loop one way declares no list": (
        _orch("cond", _if("plain", "listed"), _model("plain", "text"), _model("listed", "list"), EACH, ONE),
        {("each", "k", "items", "list")}),
    "both arms asking it": (
        _orch("cond", _if("asks", "also"), _human("asks", "decide", "approved"), _human("also", "decide", "approved"),
              DECIDE), set()),
    "a writer of unknown shape that reaches it": (
        _orch("ask", _human("ask", "fixup"), {**FIXUP, "next_step_id": "decide"}, DECIDE), set()),
    "both ways declaring a list": (
        _orch("cond", _if("plain", "listed"), _model("plain", "list"), _model("listed", "list"), EACH, ONE), set()),
}


@pytest.mark.parametrize("case", list(CASES))
def test_the_editor_says_what_the_server_says_on_every_way(node, case):
    orch, expected = CASES[case]
    assert _server(orch) == expected, _server(orch)
    assert _editor(node, orch) == expected


def test_the_editor_hands_its_checks_the_workflows_entry():
    source = VALIDATE_TS.read_text()
    assert "fieldChecks(orch.steps, agents, orch.entry_step_id)" in source


# ── a Parallel's join has every branch's keys, in the editor too ──────────────────────────────────────────────────

def _join(prompt: str, *more: dict) -> dict:
    branch = lambda sid, key: {"id": sid, "name": sid, "type": "transform", "transform_code": "result = 1",  # noqa: E731
                               "output_key": key, "next_step_id": "join"}
    return _orch("both", {"id": "both", "name": "Both", "type": "parallel", "parallel_branches": [["a"], ["b"]],
                          "next_step_id": "join"}, branch("a", "x"), branch("b", "y"),
                 {"id": "join", "name": "Join", "type": "llm", "prompt_template": prompt, "output_key": "z",
                  "next_step_id": more[0]["id"] if more else "end"}, *more)


def _unset_warnings(node, orch: dict) -> list[str]:
    found = node(VALIDATE_TS, f"return M.validateOrchestration({json.dumps(orch)}).byStep;")
    return [i["message"] for issues in found.values() for i in issues if "before any step writes it" in i["message"]]


def test_a_prompt_at_a_parallels_join_is_not_told_it_reads_a_branchs_key_empty(node):
    assert _unset_warnings(node, _join("Compare {{ state.x }} with {{ state.y }}.")) == []


def test_a_key_written_only_after_the_join_is_still_said(node):
    later = {"id": "later", "name": "Later", "type": "transform", "transform_code": "result = 2", "output_key": "w",
             "next_step_id": "end"}
    said = _unset_warnings(node, _join("Use {{ state.w }}.", later))
    assert len(said) == 1 and "“w”" in said[0], said
