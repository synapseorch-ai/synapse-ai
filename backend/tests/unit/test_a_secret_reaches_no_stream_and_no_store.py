"""A secret variable reaches the model and nothing else — F854 (BF11, D311; Naveen 2026-10-23: "fix this fully").

Found by QA1c (D314) on prod: an agent chat's live stream carried `_log_llm_call` — the agent's whole system prompt
and assembled prompt — to the browser, with `{{ global.x }}` already rendered from `variables.load()`, whose resolver
turns a secret's `synsec://` reference into its plaintext. The transcript logs (agent, orchestration) wrote the same
prompt to files the Logs screen serves; a step's `prompt_template` landed rendered in its log and progress snapshot.

The canary is a secret variable whose stored value is a reference; only the test's resolver knows the plaintext.
Each test runs a real turn or run on the fake model, then reads **every byte the test left behind** — the store's
database (checkpoints, sessions, step history), the blob store (published logs), scratch (logs, journals) and the
state dir — and every event the engine yielded. The plaintext must be in none of them, and the model must still
have received it: the prompt works, nothing shows it.
"""
from __future__ import annotations

import json
import types

from _fakes import canary
from _fakes import seed as S

# The secret and the scan, shared with the doors' tests (`tests/api_app/test_a_secret_reaches_no_door.py`).
canary_secret = canary.canary_secret
CANARY, MARK = canary.CANARY, canary.MARK
_left_behind = canary.left_behind


def _server():
    # A chat turn refuses with "No agents connected" on an empty session map.
    return types.SimpleNamespace(agent_sessions={"x": object()}, memory_store=None, tool_router={})


class TestAChatTurn:
    async def test_the_model_gets_the_secret_and_no_event_or_file_holds_it(
            self, canary_secret, fake_llm, tmp_path):
        from core.models import ChatRequest
        from core.react_engine import run_react_loop

        agent = S.make_agent(
            id="agent_canary", name="Billing", tools=["all"], skip_default_tools=True,
            system_prompt="You call the billing API with key {{ global.API_TOKEN }} in {{ global.REGION }}.")
        await S.seed_agents([agent])
        fake_llm.set_default("Done.")

        events = [e async for e in run_react_loop(
            ChatRequest(message="what is owed?", session_id="chat_canary", agent_id=agent["id"]), _server())]

        assert CANARY in fake_llm.calls[0]["sys_prompt"], "the prompt still works"
        assert CANARY not in json.dumps(events, default=str), sorted({e.get("type") for e in events if CANARY in json.dumps(e, default=str)})
        assert _left_behind(tmp_path) == [], _left_behind(tmp_path)


    async def test_a_secret_the_person_types_is_logged_and_kept_by_name_only(
            self, canary_secret, fake_llm, tmp_path):
        """The canary in the person's own message, not only the prompt (F1184): the agent log's banner is written
        as the turn starts, so the variables are read, and the secret held, before it."""
        from core.models import ChatRequest
        from core.react_engine import run_react_loop

        agent = S.make_agent(id="agent_canary_typed", name="Billing", tools=["all"], skip_default_tools=True,
                             system_prompt="You bill in {{ global.REGION }}.")
        await S.seed_agents([agent])
        fake_llm.set_default("Done.")

        events = [e async for e in run_react_loop(
            ChatRequest(message=f"my key is {CANARY}, use it", session_id="chat_canary_typed",
                        agent_id=agent["id"]), _server())]

        assert CANARY in json.dumps(fake_llm.calls[0], default=str), "the model reads the message as sent"
        assert CANARY not in json.dumps(events, default=str)
        assert _left_behind(tmp_path) == [], _left_behind(tmp_path)


class TestAWorkflowRun:
    async def test_a_step_prompt_with_the_secret_is_shown_and_stored_by_name_only(
            self, canary_secret, fake_llm, tmp_path):
        from core.models_orchestration import Orchestration
        from core.orchestration.engine import OrchestrationEngine

        agent = S.make_agent(id="agent_canary_step", name="Billing", tools=["all"], skip_default_tools=True,
                             system_prompt="You are terse. Region {{ global.REGION }}; key {{ global.API_TOKEN }}.")
        orch = S.make_orchestration(id="orch_canary", entry_step_id="ask", steps=[
            {"id": "ask", "name": "Ask", "type": "agent", "agent_id": agent["id"],
             "prompt_template": "Fetch the invoice using token {{ global.API_TOKEN }}.", "output_key": "answer",
             "next_step_id": None},
        ])
        await S.seed_agents([agent])
        await S.seed_orchestrations([orch])
        fake_llm.set_default("Invoice 42.")

        engine = OrchestrationEngine(Orchestration.model_validate(orch), _server())
        events = [e async for e in engine.run("go", run_id="run_canary")]

        assert any(CANARY in (c.get("sys_prompt") or "") for c in fake_llm.calls), "the agent's prompt still works"
        assert any(CANARY in json.dumps(c, default=str) for c in fake_llm.calls), "the step's prompt still works"
        assert next(e for e in reversed(events) if e.get("type") == "orchestration_complete")["status"] == "completed"
        assert CANARY not in json.dumps(events, default=str), sorted({e.get("type") for e in events if CANARY in json.dumps(e, default=str)})
        assert _left_behind(tmp_path) == [], _left_behind(tmp_path)

    async def test_a_failure_whose_words_hold_the_secret_records_it_by_name(self, canary_secret):
        """`reason_of` writes a step's failure to step_history and `step_error.reason`."""
        from core import variables
        from core.orchestration.run_error import reason_of

        await variables.load()
        said = reason_of(RuntimeError(f"Could not reach https://{CANARY}.example/x"))
        assert CANARY not in said and MARK in said


class TestTheBuildersVariables:
    async def test_neither_a_secret_nor_a_plain_value_is_shown(self, canary_secret):
        """`list_variables` showed the first 40 characters of every value: cut, a long secret no longer matched
        the value held to scrub, and its start reached the model and the builder's tool result (SAI2's OK). A plain
        value was shown as it was; since F1271 no value is, only names and their references."""
        from core.builder_tools import execute_builder_tool

        raw = await execute_builder_tool("list_variables", {}, _server())
        listed = json.loads(raw)
        assert {v["key"]: v["reference"] for v in listed} == {"API_TOKEN": "{{ global.API_TOKEN }}",
                                                               "REGION": "{{ global.REGION }}"}
        assert all(set(v) == {"key", "reference"} for v in listed)
        assert CANARY not in raw and "eu-west-1" not in raw
