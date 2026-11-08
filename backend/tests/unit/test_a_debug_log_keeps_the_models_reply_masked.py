"""F1476: the agent loop's DEBUG line for the model's reply shows the run's secrets by name.

`🤖 LLM Response` printed up to 500 characters of the reply as it came. A reply that calls a tool carries the call's
arguments, and a model can say a key it read, so a held secret reached the log, where F854's masking does not reach.
The line is the one that says what the model did, so it is kept and scrubbed, not cut to a size as F1469 and F1475
did; and only when DEBUG is on, so a workspace at INFO pays nothing for it."""
from __future__ import annotations

import logging
import types

import pytest

from _fakes import seed as S

#: A workspace secret: a tool's header holds it, so each turn's matcher marks it (F1474).
HELD = "sk-held-5Rv8Nq3Lx7Tz1Wp"
REPLY = f"The key on file is {HELD}, and it still works."


@pytest.fixture
async def held_header():
    from core.store.resources import save_tool

    await save_tool({"id": "tool_f1476", "name": "poster", "type": "http", "url": "https://post.test/",
                     "method": "POST", "headers": {"Authorization": f"Bearer {HELD}"}})


@pytest.fixture
def scrubbed(monkeypatch):
    """Every text react_engine hands its scrub, in order."""
    import core.react_engine as re_mod

    seen: list[str] = []
    real = re_mod._scrub_secrets

    def recording(text):
        seen.append(text)
        return real(text)

    monkeypatch.setattr(re_mod, "_scrub_secrets", recording)
    return seen


async def _turn(fake_llm):
    from core.react_engine import run_agent_step

    fake_llm.script([REPLY])
    agent = S.make_agent(tools=[], skip_default_tools=True)
    return [e async for e in run_agent_step(
        message="Is the key still good?", agent_id=agent["id"], session_id="s_f1476",
        server_module=types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={}),
        agent_override=agent, tools_override=[], max_turns=2)]


async def test_the_replys_debug_line_shows_a_held_secret_by_its_mark(fake_llm, held_header, scrubbed, caplog):
    from core.secret_scrub import CREDENTIAL_MARK

    caplog.set_level(logging.DEBUG, logger="core.react_engine")
    await _turn(fake_llm)

    lines = [r.getMessage() for r in caplog.records
             if r.name == "core.react_engine" and r.getMessage().startswith("🤖 LLM Response")]
    assert lines, "no reply line was logged"
    assert not [m for m in lines if HELD in m], lines
    assert any(CREDENTIAL_MARK in m for m in lines), lines
    assert REPLY in scrubbed


async def test_at_info_the_reply_is_never_scrubbed_for_the_log(fake_llm, held_header, scrubbed, caplog):
    caplog.set_level(logging.INFO, logger="core.react_engine")
    await _turn(fake_llm)

    assert not [r for r in caplog.records if r.getMessage().startswith("🤖 LLM Response")]
    assert REPLY not in scrubbed, "the reply was scrubbed for a line that is not logged"
