"""A secret the workspace holds, typed into a workflow's own Human step, is kept nowhere (F1307, TRI1; 79's question and
Naveen's 11-03 choice: cover workflows' answers too).

The builder's door left any other run's answer untouched (BWA1), so a held value pasted into a workflow's Human step —
answered on the run page, by v1, MCP or a channel, all through `answer_paused_run` — was kept raw in the run's state
and logs. A workflow already has that value as a variable or a connection, so pasting it is a mistake: it now loses the
held secrets by their exact value, and nothing else — its other words stay the workflow's own, as F1100 keeps a
workflow's card digits.

Every secret below is assembled at run time: a literal one in a source file trips secret scanners.
"""
from __future__ import annotations

import json

import pytest

from core.native_builder.door import CREDENTIAL_MARK
from tests.api_app.test_mcp_server import mcp_app  # noqa: F401 — fixture
from tests.api_v1.test_v1_run_control import _until_status

CANARY = "tracker-" + "canary-token-" + "4410"

_ASK = [
    {"id": "ask", "name": "Ask", "type": "human", "human_prompt": "Anything to add?", "output_key": "answer",
     "human_fields": [{"name": "note", "type": "textarea", "label": "Note"}], "next_step_id": "s"},
    {"id": "s", "name": "Think", "type": "llm", "prompt_template": "hi", "output_key": "out", "next_step_id": None},
]


@pytest.fixture
async def held():
    from core.store.resources import save_mcp_server

    await save_mcp_server({"name": "tracker", "label": "Tracker", "server_type": "http",
                           "url": "http://localhost:9/mcp", "token": CANARY})


@pytest.fixture
async def asking(client, api_key, seed_orchestration):
    orch = await seed_orchestration(entry_step_id="ask", steps=_ASK)
    started = await client.post(f"/api/v1/orchestrations/{orch['id']}/run", json={"message": "go"},
                                headers=api_key["headers"])
    run_id = started.json()["run_id"]
    assert await _until_status(run_id, "paused")
    return run_id


async def test_v1s_answer_loses_the_held_secret_and_keeps_the_rest(client, api_key, held, asking):
    from core.orchestration.state import SharedState

    resp = await client.post(f"/api/v1/orchestrations/runs/{asking}/resume",
                             json={"response": {"note": f"use {CANARY} for it, card 4242"}}, headers=api_key["headers"])
    assert resp.status_code == 200, resp.text
    state = (await SharedState.restore(asking)).run.shared_state
    assert state["answer"]["note"] == f"use {CREDENTIAL_MARK} for it, card 4242", "only the held value comes out"
    assert CANARY not in json.dumps(state, default=str, ensure_ascii=False)


async def test_the_apps_answer_too(client, held, asking):
    from core.orchestration.state import SharedState

    resp = await client.post(f"/api/orchestrations/runs/{asking}/human-input", json={"response": {"note": CANARY}})
    assert resp.status_code in (200, 202), resp.text
    state = (await SharedState.restore(asking)).run.shared_state
    assert state["answer"]["note"] == CREDENTIAL_MARK


async def test_an_answer_with_nothing_held_is_the_workflows_own(client, api_key, asking):
    from core.orchestration.state import SharedState

    other = "not-" + "held-value-" + "5150"
    resp = await client.post(f"/api/v1/orchestrations/runs/{asking}/resume",
                             json={"response": {"note": other}}, headers=api_key["headers"])
    assert resp.status_code == 200, resp.text
    assert (await SharedState.restore(asking)).run.shared_state["answer"]["note"] == other
