"""A step that fails reading what a Tool step's answer left out says which tool left it out (F1620, part 2b).

A Tool step whose answer lacks a field its tool declares says so as a warning and keeps the answer as given (2a). A later
step reading what is missing still failed with only its own words ("KeyError: 'email'"), and a condition reading it
took its unwritten answer with only the evaluator's words (F1433): neither said why. Now the Tool step's answer key is
recorded with the paths it lacked, paths only and never a value, privately, and dropped when that step's next answer
holds. A step that fails on a lookup (a missing key or index, an attribute or a type a value does not have), or a
condition's warning about a value not written, adds one bounded clause naming the Tool step, its tool and what it left
out, when that step reads the key there.

Near misses: a failure that is not a lookup, a lookup in a step that does not read the missed key, and a lookup on a
key whose answer held say nothing more; two keys missed by two tools each name their own tool; a Loop whose next answer
holds clears the record; a long list of misses names five; a condition on a key that held says nothing more.
"""
from __future__ import annotations

import json
import types

import pytest

from _fakes import seed as S
from core.orchestration.output_fields import json_schema
from core.tools import VirtualTool

LEADS = [{"name": "leads", "type": "list", "of": {"fields": [{"name": "email", "type": "string"}]}},
         {"name": "total", "type": "number"}]
SCHEMA = json_schema(LEADS)
WIDE = json_schema([{"name": f"f{i}", "type": "string"} for i in range(8)])
TAKES = {"type": "object", "properties": {"week": {"type": "string"}}}
GOOD = {"leads": [{"email": "a@example.com"}, {"email": "b@example.com"}], "total": 2}
LACKS = {"leads": [{"email": "a@example.com"}, {"name": "Harbor & Pine"}], "total": 2}


def _ann():
    from mcp.types import ToolAnnotations
    return ToolAnnotations(readOnlyHint=True, idempotentHint=True)


def _tool(name: str, schema: dict) -> VirtualTool:
    tool = VirtualTool(name, "lists leads", TAKES, _ann())
    tool.outputSchema = schema
    return tool


class _Session:
    def __init__(self):
        self.answers: dict[str, list[str]] = {}

    async def call_tool(self, name, args, **_kw):
        queue = self.answers[name]
        text = queue.pop(0) if len(queue) > 1 else queue[0]
        return types.SimpleNamespace(content=[types.SimpleNamespace(text=text)], isError=False)


@pytest.fixture
def server(monkeypatch):
    """`crm_leads` and `crm_deals`, MCP tools declaring LEADS; `crm_wide`, one declaring eight text fields."""
    import core.tools as tools_mod

    declared = {"crm_leads": _tool("crm_leads", SCHEMA), "crm_deals": _tool("crm_deals", SCHEMA),
                "crm_wide": _tool("crm_wide", WIDE)}

    async def one(server_module, name, agent, custom_tools):
        return declared.get(name)

    monkeypatch.setattr(tools_mod, "resolve_one_tool", one)
    session = _Session()
    return types.SimpleNamespace(agent_sessions={"fake": session}, memory_store=None,
                                 tool_router={n: ("fake", n) for n in declared}, session=session)


def _call(sid: str, tool: str, key: str, nxt: str | None, name: str = "Find leads") -> dict:
    return {"id": sid, "name": name, "type": "tool", "forced_tool": tool, "tool_arguments": {"week": "this"},
            "output_key": key, "model": "claude-x", "next_step_id": nxt}


def _code(code: str, sid: str = "parse", nxt: str | None = None) -> dict:
    return {"id": sid, "name": "Read the lead", "type": "transform", "transform_code": code, "output_key": sid,
            "next_step_id": nxt}


async def _run(server, steps: list[dict], answers: dict[str, list], run_id: str):
    from core.models_orchestration import Orchestration
    from core.orchestration.engine import OrchestrationEngine

    for tool, said in answers.items():
        server.session.answers[tool] = [a if isinstance(a, str) else json.dumps(a) for a in said]
    orch = S.make_orchestration(id=f"orch_{run_id}", entry_step_id=steps[0]["id"], steps=steps)
    await S.seed_orchestrations([orch])
    events = [e async for e in OrchestrationEngine(Orchestration.model_validate(orch), server).run("go", run_id=run_id)]
    done = [e for e in events if e.get("type") == "orchestration_complete"][-1]
    failed = [e.get("reason") or "" for e in events if e.get("type") == "step_error"]
    warned = {e["orch_step_id"]: e["message"] for e in events if e.get("type") == "step_warning"}
    return done, failed, warned


READ_EMAIL = "import json\nresult = json.loads(state['found'])['leads'][1]['email']"


class TestTheFailureSaysWhichToolLeftItOut:
    async def test_a_lookup_of_what_the_answer_lacks_names_the_tool_and_the_path(self, server):
        done, failed, _ = await _run(server, [_call("find", "crm_leads", "found", "parse"), _code(READ_EMAIL)],
                                     {"crm_leads": [LACKS]}, "run_f1620b_named")
        assert done["status"] == "failed" and len(failed) == 1, (done["status"], failed)
        assert "KeyError" in failed[0], "the step's own words stay first"
        assert "`leads[1].email`" in failed[0] and "crm_leads" in failed[0] and "Find leads" in failed[0], failed[0]

    async def test_two_keys_missed_by_two_tools_each_name_their_own(self, server):
        steps = [_call("find", "crm_leads", "found", "deals"),
                 _call("deals", "crm_deals", "deals", "parse", name="Find deals"),
                 _code("import json\nresult = json.loads(state['deals'])['total'] + 1")]
        _, failed, _ = await _run(server, steps, {"crm_leads": [LACKS], "crm_deals": [{"leads": []}]},
                                  "run_f1620b_two")
        assert len(failed) == 1 and "crm_deals" in failed[0] and "`total`" in failed[0], failed
        assert "crm_leads" not in failed[0] and "leads[1].email" not in failed[0], failed[0]

    async def test_the_record_holds_paths_never_values(self, server):
        key = "sk-abcdefghijklmnopqrstuvwxyz123456"
        steps = [_call("find", "crm_leads", "found", "parse"),
                 _code("import json\nresult = json.loads(state['found'])['total'] + 1")]
        done, failed, _ = await _run(server, steps, {"crm_leads": [{"leads": [], "total": key}]}, "run_f1620b_key")
        assert len(failed) == 1 and "`total`" in failed[0] and key[:12] not in failed[0], failed
        record = done["final_state"]["_answers_missed"]
        assert "total" in json.dumps(record) and key[:12] not in json.dumps(record), record

    async def test_a_long_list_of_misses_names_five(self, server):
        steps = [_call("find", "crm_wide", "found", "parse"),
                 _code("import json\nresult = json.loads(state['found'])['f0']")]
        _, failed, _ = await _run(server, steps, {"crm_wide": [{}]}, "run_f1620b_wide")
        assert len(failed) == 1 and "`f4`" in failed[0] and "`f5`" not in failed[0] and "3 more" in failed[0], failed


class TestAConditionsWarningSaysItToo:
    def _steps(self, condition: str) -> list[dict]:
        return [_call("find", "crm_leads", "found", "hot"),
                {"id": "hot", "name": "Hot lead?", "type": "if_else", "if_condition": condition,
                 "if_true_step_id": None, "if_false_step_id": None}]

    async def test_a_conditions_unwritten_read_names_the_tool(self, server):
        _, _, warned = await _run(server, self._steps("state.found.leads[1].email.lower() == 'vip'"),
                                  {"crm_leads": [LACKS]}, "run_f1620b_cond")
        assert "crm_leads" in warned["hot"] and "`leads[1].email`" in warned["hot"], warned

    async def test_a_condition_on_a_key_that_held_says_nothing_more(self, server):
        _, _, warned = await _run(server, self._steps("state.found.leads[5].email == 'vip'"),
                                  {"crm_leads": [GOOD]}, "run_f1620b_cond_held")
        assert "hot" in warned and "crm_leads" not in warned["hot"], warned


class TestNearMisses:
    async def test_a_failure_that_is_no_lookup_says_nothing_more(self, server):
        code = "import json\nn = len(json.loads(state['found'])['leads'])\nresult = n / 0"
        _, failed, _ = await _run(server, [_call("find", "crm_leads", "found", "parse"), _code(code)],
                                  {"crm_leads": [LACKS]}, "run_f1620b_zero")
        assert len(failed) == 1 and "ZeroDivisionError" in failed[0] and "crm_leads" not in failed[0], failed

    async def test_a_lookup_in_a_step_that_does_not_read_the_key_says_nothing_more(self, server):
        _, failed, _ = await _run(server, [_call("find", "crm_leads", "found", "parse"),
                                           _code("result = {}['nothing']")],
                                  {"crm_leads": [LACKS]}, "run_f1620b_other")
        assert len(failed) == 1 and "KeyError" in failed[0] and "crm_leads" not in failed[0], failed

    async def test_a_lookup_on_a_key_whose_answer_held_says_nothing_more(self, server):
        code = "import json\nresult = json.loads(state['found'])['leads'][5]['email']"
        _, failed, _ = await _run(server, [_call("find", "crm_leads", "found", "parse"), _code(code)],
                                  {"crm_leads": [GOOD]}, "run_f1620b_held")
        assert len(failed) == 1 and "IndexError" in failed[0] and "crm_leads" not in failed[0], failed

    async def test_a_loop_whose_next_answer_holds_clears_the_record(self, server):
        steps = [_code("result = ['first', 'second']", sid="items", nxt="each"),
                 {"id": "each", "name": "Each week", "type": "loop", "loop_over": "state.items",
                  "loop_item_key": "item", "loop_step_ids": ["find"], "next_step_id": "parse"},
                 _call("find", "crm_leads", "found", None),
                 # After the Loop, its body's key holds each pass's `result`: the last is the one that held.
                 _code("import json\nfound = state['found']\n"
                       "last = found[-1]['result'] if isinstance(found, list) else found\n"
                       "last = json.loads(last) if isinstance(last, str) else last\n"
                       "result = last['leads'][7]['email']")]
        done, failed, _ = await _run(server, steps, {"crm_leads": [LACKS, GOOD]}, "run_f1620b_loop")
        assert len(failed) == 1 and "IndexError" in failed[0] and "crm_leads" not in failed[0], failed
        assert not (done["final_state"].get("_answers_missed") or {}).get("found"), done["final_state"].get(
            "_answers_missed")
