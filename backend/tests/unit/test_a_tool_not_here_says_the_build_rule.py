"""A read for a tool that is not here says the build rule, not only where else to look (F1495, B; TRI2's class 2, pro
drafts/tri2/TRIAGE.md; Naveen's pick 11-10).

TRI2's prod run (run_01a1bf14…, 11-09): the planner, on Sonnet 4.6, asked `read_workspace {topic: tool, name: github}`
three times and `GitHub` twice, then `agents`, `models` and `settings`. Each answer said "No custom tool is called
'github'. This shows one custom tool … Topic `agents` lists the agents …": where else to look, and never that a
service's tool nothing here provides is not looked for at all (planner.json, # Building 1: "the tool still goes in
the plan, named for what it does … code finds out who provides it"). Now the topic's sentence says it, the same words
every time, so the repeat guard still sees a repeat (F1237).
"""
from __future__ import annotations

from core.builder_readers import ELSEWHERE

from test_the_assistant_reads_what_happened import _read, viewer  # noqa: F401

RULE = ("A tool nothing here provides is never looked for: a plan names it for what it does, with the service in the "
        "step's words, and code looks up who provides it.")


async def test_a_tool_not_here_says_the_rule(viewer):
    got = await _read(topic="tool", name="github")
    assert got["error"].startswith("No custom tool is called 'github'."), got
    assert got["error"].endswith(ELSEWHERE["tool"]), got
    assert RULE in got["error"], got


async def test_the_same_words_every_time(viewer):
    """The repeat guard keys a read by its result (F1236): the rule never varies with the name asked."""
    assert RULE in ELSEWHERE["tool"]
    assert await _read(topic="tool", name="github") == await _read(topic="tool", name="github")
