"""The chat redraws a reopened question from the object its live event carried (F1333, TRI1; Naveen 11-04).

The panel draws a review's card from its question's object (`context`, F795) and, without one, from its words
(`readPlan`). A chat read back from its runs (`builderReattach.reattachedState`) pushed the question with no object, and
a question whose journal event was shrunk past its size was re-read from the run (`builderChats.fromTheRun`) without it,
so both drew the words. Each now passes on the object the server reads back: the conversation's `pending.context` and
`context_key`, the run's `human_context` and `human_context_key`. Read from the source, as the frontend's other pins are.
"""
from __future__ import annotations

from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[3] / "frontend" / "src"


def test_a_conversation_read_back_hands_the_question_its_object():
    source = (FRONTEND / "lib" / "builderReattach.ts").read_text()
    pending = source[source.index("pending: {"):source.index("} | null;", source.index("pending: {"))]
    assert "context_key?: string | null" in pending and "context?: Record<string, unknown>" in pending
    pushed = source[source.index("kind: 'human_prompt', key: `back-asked-"):source.index("});", source.index(
        "kind: 'human_prompt', key: `back-asked-"))]
    assert "contextKey: asked.context_key" in pushed and "context: asked.context" in pushed


def test_a_shrunk_question_re_read_from_its_run_keeps_its_object():
    source = (FRONTEND / "components" / "app" / "builderChats.ts").read_text()
    reread = source[source.index("if (event.type === 'human_input_required') {"):source.index(
        "} catch {", source.index("if (event.type === 'human_input_required') {"))]
    assert "context_key: run.human_context_key" in reread and "context: run.human_context" in reread
