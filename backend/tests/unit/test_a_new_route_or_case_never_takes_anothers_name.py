"""A new Evaluator route or Switch case never takes another's name — F1559 (HUM1; Naveen 11-12, after F1557).

An Evaluator's routes and a Switch's cases are a mapping by name (`route_map`, `switch_cases`), and the editor's Add
route and Add case named the new one after the count: `route_<n + 1>`, `case_<n + 1>`. After a delete that is a name
the step still has, and a mapping keeps one value per name, so Add replaced that route's target with none — the
route the person had wired went to End — and Add case did the same to a case. F1557's Add field did it to a Human
step's fields.

Now each takes the first name from the count up that the step does not have (`nextName.nextFreeName`), as F1557's Add
field does.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
NEXT_TS = ROOT / "frontend/src/components/orchestration/nextName.ts"
STEPS_TSX = ROOT / "frontend/src/components/orchestration/panel/steps.tsx"


def _node(script: str):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not on PATH: the editor's Add route and Add case are TypeScript")
    run = subprocess.run([node, "--experimental-strip-types", "--no-warnings", "--input-type=module", "-e",
                          f"import * as N from {json.dumps(str(NEXT_TS))};\nconsole.log(JSON.stringify({script}));"],
                         capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr[-2000:]
    return json.loads(run.stdout)


@pytest.mark.parametrize("prefix, taken, name", [
    ("route_", [], "route_1"),
    ("route_", ["route_1", "route_2"], "route_3"),            # as before, with nothing deleted
    ("route_", ["route_2"], "route_3"),                       # route_1 deleted: never route_2 again
    ("route_", ["approved", "rejected"], "route_3"),          # renamed routes: the count, as before
    ("case_", ["case_2", "case_3"], "case_4"),
    ("case_", ["urgent", "case_3", "case_4"], "case_5"),
])
def test_the_next_name_is_one_the_step_does_not_have(prefix, taken, name):
    assert _node(f"N.nextFreeName({json.dumps(prefix)}, {json.dumps(taken)})") == name


def test_add_route_and_add_case_take_it():
    steps = STEPS_TSX.read_text()
    assert "existing.length + 1" not in steps, "a name from the count alone"
    assert "nextFreeName('route_', Object.keys(step.route_map || {}))" in steps
    assert "nextFreeName('case_', Object.keys(step.switch_cases || {}))" in steps
    assert "from '../nextName'" in steps
