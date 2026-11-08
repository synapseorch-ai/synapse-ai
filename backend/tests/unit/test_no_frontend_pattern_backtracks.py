"""No pattern in the web app can take exponential time on the text it reads (F1470, CodeQL js/redos on PR #379).

The run page read a nested workflow's failure with `(?: › '.+?')*`: a repeated group around a lazy wildcard, so each
"' › '" in the reason could be split many ways, and a long chain that never said how it ended hung the tab. A
failure reason carries workflow names and a tool's or an API's own words. The chain is now read by position
(`nestedChain` in frontend/src/components/run/failures.ts), and no pattern in the frontend repeats a group that holds
a lazy wildcard. The pattern's own timing is held by the web app's vitest (runFailuresNested.test.ts).
"""
from __future__ import annotations

import re
from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[3] / "frontend" / "src"
#: A group with `.+?` or `.*?` in it, repeated by `*` or `+`.
REPEATED_LAZY_WILDCARD = re.compile(r"\((?:\?:)?[^()]*\.[+*]\?[^()]*\)[*+]")


def _sources():
    for path in sorted(FRONTEND.rglob("*")):
        if path.suffix in {".ts", ".tsx"} and "node_modules" not in path.parts:
            yield path


def test_no_pattern_repeats_a_group_around_a_lazy_wildcard():
    found = [f"{path.relative_to(FRONTEND)}:{n}: {line.strip()[:120]}"
             for path in _sources()
             for n, line in enumerate(path.read_text("utf-8").splitlines(), 1)
             if REPEATED_LAZY_WILDCARD.search(line)]
    assert not found, "\n".join(found)


def test_the_check_sees_the_pattern_that_hung_the_tab():
    assert REPEATED_LAZY_WILDCARD.search(r"/Sub-orchestration '(.+?)'((?: › '.+?')*) (failed|was cancelled): /")


def test_a_nested_failure_is_read_by_position():
    source = (FRONTEND / "components" / "run" / "failures.ts").read_text("utf-8")
    assert "export function nestedChain(" in source
    assert "const chain = nestedChain(raw);" in source
