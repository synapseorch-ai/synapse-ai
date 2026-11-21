"""An agent step whose answer a later step reads is told its answer is all that step gets, and is warned when it hands
on far less than its tools found (F1677).

An Agent step's output is its final answer, nothing else. A site reader whose tools fetched thirteen pages (about 160K
characters) answered with a one-line status, so the next step read only the status and guessed the rest: it said so
itself. Now an agent step that a later step reads (by its input keys, a `{{ state.x }}` in its prompt, or a
Transform's code) is told so in its system prompt; and when its answer comes back far shorter than what its tools
returned (under 2,000 characters, against 20,000 or more, a tenth or less), the step warns, naming both sizes. The
warning never stops the run.

Near misses: no line and no warning for a step nothing later reads; no warning for an answer that carries what was
found, for tools that returned little, or for a step that declares its answer's fields.
"""
from __future__ import annotations

import types
from typing import Any

from _fakes import seed as S
from _fakes.fake_llm import tool_call

PAGE = "".join(f"Page line {n:05d}: Harbor & Pine's catalogue, made up for a test, padded to length.\n"
               for n in range(400))  # ~32K characters
STATUS = "Done — I read the site's pages."
SAID_LINE = "your final answer is all it gets"


class _Site:
    async def send_ping(self):
        return None

    async def call_tool(self, name, args, read_timeout_seconds=None):
        return types.SimpleNamespace(content=[types.SimpleNamespace(text=self.text)], isError=False)

    def __init__(self, text: str = PAGE) -> None:
        self.text = text


class _Model:
    """The model, recording each call's system prompt; replies in order, the last one repeating."""

    def __init__(self, replies: list[Any]) -> None:
        self.replies = list(replies)
        self.systems: list[str] = []

    async def __call__(self, *args: Any, **kw: Any):
        self.systems.append(str(kw.get("sys_prompt") or (args[1] if len(args) > 1 else "")))
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]


def _install(monkeypatch, model: _Model) -> None:
    import core.llm_providers as _llm
    import core.react_engine as _re
    import core.routes.agents as _agents

    monkeypatch.setattr(_llm, "generate_response", model, raising=False)
    monkeypatch.setattr(_re, "llm_generate_response", model, raising=False)
    monkeypatch.setattr(_agents, "llm_generate_response", model, raising=False)


async def _run(monkeypatch, *, answer: str = STATUS, page: str = PAGE, read_later: bool = True,
               fields: list | None = None, tag: str) -> tuple[list[dict], _Model]:
    from core.models_orchestration import Orchestration
    from core.orchestration.engine import OrchestrationEngine

    model = _Model([tool_call("site__read_pages"), answer, "The page list."])
    _install(monkeypatch, model)
    agent = S.make_agent(name="Site Reader", tools=["site__read_pages"], skip_default_tools=True, max_turns=6)
    await S.seed_agents([agent])
    reader = {"id": "use", "name": "Use the pages", "type": "llm", "model": "mistral",
              "prompt_template": "List the pages.", "input_keys": ["site"], "output_key": "pages", "next_step_id": None}
    read = {"id": "read", "name": "Read the site", "type": "agent", "agent_id": agent["id"],
            "prompt_template": "Read the site's pages.", "output_key": "site",
            "next_step_id": "use" if read_later else None, **({"output_fields": fields} if fields else {})}
    orch = S.make_orchestration(id=f"orch_f1677_{tag}", entry_step_id="read",
                                steps=[read, *([reader] if read_later else [])])
    await S.seed_orchestrations([orch])
    server = types.SimpleNamespace(agent_sessions={"ext_mcp_site": _Site(page)}, memory_store=None,
                                   tool_router={"site__read_pages": ("ext_mcp_site", "read_pages")})
    engine = OrchestrationEngine(Orchestration.model_validate(orch), server)
    events = [e async for e in engine.run("go", run_id=f"run_f1677_{tag}")]
    # Every case really ran its tool, so a "not warned" never passes for want of a tool result.
    assert any(e.get("type") == "tool_result" and e.get("tool_name") == "site__read_pages" for e in events), events
    return events, model


def _warnings(events: list[dict]) -> list[str]:
    return [str(e.get("message")) for e in events if e.get("type") == "step_warning"]


# ── (a) told that its answer is handed on ─────────────────────────────────────────────────────────────────────────

async def test_an_agent_a_later_step_reads_is_told_its_answer_is_all_it_gets(monkeypatch):
    _, model = await _run(monkeypatch, tag="told")
    assert any(SAID_LINE in s for s in model.systems[:2]), model.systems[:1]


async def test_an_agent_nothing_later_reads_is_not_told(monkeypatch):
    _, model = await _run(monkeypatch, read_later=False, tag="untold")
    assert not any(SAID_LINE in s for s in model.systems), "no step reads it"


# ── (b) warned when it hands on far less than its tools found ─────────────────────────────────────────────────────

async def test_a_status_after_a_large_tool_result_is_warned_with_both_sizes(monkeypatch):
    events, _ = await _run(monkeypatch, tag="status")
    said = [w for w in _warnings(events) if "its tools returned" in w]
    assert len(said) == 1, _warnings(events)
    assert f"{len(STATUS)} characters" in said[0] and f"{len(PAGE):,}" in said[0], said
    assert any(e.get("type") == "orchestration_complete" for e in events), "a warning never stops the run"


async def test_an_answer_that_carries_what_was_found_is_not_warned(monkeypatch):
    events, _ = await _run(monkeypatch, answer=PAGE[:8000], tag="carries")
    assert not [w for w in _warnings(events) if "its tools returned" in w]


async def test_tools_that_returned_little_are_not_warned(monkeypatch):
    events, _ = await _run(monkeypatch, page="Two pages: /about and /pricing.", tag="little")
    assert not [w for w in _warnings(events) if "its tools returned" in w]


async def test_a_step_that_declares_its_answers_fields_is_not_warned(monkeypatch):
    """A declared answer is short by design: a count read off a long page is the answer, not a status."""
    events, _ = await _run(monkeypatch, answer='{"pages": 13}', fields=[{"name": "pages", "type": "number"}],
                           tag="fields")
    assert not [w for w in _warnings(events) if "its tools returned" in w]


async def test_a_step_nothing_later_reads_is_not_warned(monkeypatch):
    events, _ = await _run(monkeypatch, read_later=False, tag="unread")
    assert not [w for w in _warnings(events) if "its tools returned" in w]
