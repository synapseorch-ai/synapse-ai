"""The run page's "The result" reads a run's result by the engine's own rule (F901, POL1, D317).

`core/orchestration/results.py` decides what a run made (F249): the workflow's `result_key` when written, else the
last step that produced content, never a person's answer, a route or an End. The run page (`components/run/model.ts`
`resultOf`) says the same in TypeScript, so the step types it skips are pinned to the engine's here.
"""
from __future__ import annotations

import re
from pathlib import Path

from core.orchestration.results import NOT_CONTENT

FRONTEND = Path(__file__).resolve().parents[3] / "frontend" / "src"


def test_the_page_skips_the_engine_s_step_types():
    source = (FRONTEND / "components" / "run" / "model.ts").read_text("utf-8")
    found = re.search(r"export const NOT_CONTENT: readonly string\[\] = \[([^\]]*)\];", source)
    assert found, "NOT_CONTENT is gone from frontend/src/components/run/model.ts"
    assert set(re.findall(r"'([a-z_]+)'", found.group(1))) == set(NOT_CONTENT)
