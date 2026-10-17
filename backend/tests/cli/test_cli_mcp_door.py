"""What a key reaches only through the product tools, from the CLI: build, delete, Boards, what waits, activity,
usage, and any tool or route by name (CLI1, D294).

Naveen's call (2026-10-17): the CLI calls MCP's endpoint for these, with the same key, so the registry's role,
scope, confirmation and audit hold for it as for Claude, and no key route is added (D124). Each test drives the
real CLI against the live app with its MCP endpoint running.
"""
from __future__ import annotations

import json

import pytest

from cli_harness import answers
from synapse.client import errors

pytestmark = pytest.mark.usefixtures("mcp_endpoint")

_STEPS = [{"id": "s", "name": "Think", "type": "llm", "prompt_template": "hi", "output_key": "out",
           "next_step_id": None}]
#: One that would pass `validate`: its path reaches an End step.
_WHOLE = [{**_STEPS[0], "next_step_id": "end"}, {"id": "end", "name": "End", "type": "end"}]


def _file(tmp_path, name, value) -> str:
    path = tmp_path / name
    path.write_text(json.dumps(value))
    return str(path)


# ── agents ──────────────────────────────────────────────────────────────────


class TestAgents:
    async def test_made_changed_and_deleted_once_asked(self, synapse, fake_llm, monkeypatch):
        from core.store.resources import get_agent

        code, out, err = await synapse("agents", "create", "--name", "Scribe", "--prompt", "You take notes.",
                                       "--description", "Notes")
        assert code == 0, err
        agent_id = out.strip()
        made = await get_agent(agent_id)
        assert made["name"] == "Scribe" and made["system_prompt"] == "You take notes."
        assert "synapse chat --agent" in err

        code, out, err = await synapse("agents", "update", "scribe", "--prompt", "You take short notes.",
                                       "--set", "max_turns=4")
        assert code == 0, err
        changed = await get_agent(agent_id)
        assert changed["system_prompt"] == "You take short notes." and changed["max_turns"] == 4

        code, _, err = await synapse("agents", "delete", agent_id)
        assert code == errors.USAGE and "--yes" in err, "a script is asked first, and cannot answer"
        assert await get_agent(agent_id) is not None

        answers(monkeypatch, "n")
        code, _, err = await synapse("agents", "delete", agent_id)
        assert code == 0 and "Kept." in err and await get_agent(agent_id) is not None

        code, out, _ = await synapse("agents", "delete", agent_id, "--yes")
        assert code == 0 and out.strip() == "Deleted."
        assert await get_agent(agent_id) is None

    async def test_a_delete_refused_says_what_uses_it(self, synapse, seed_agent, seed_orchestration):
        agent = await seed_agent(name="Worker")
        await seed_orchestration(name="Uses it", entry_step_id="s",
                                 steps=[{"id": "s", "name": "Work", "type": "agent", "agent_id": agent["id"],
                                         "prompt_template": "go", "next_step_id": None}])
        code, _, err = await synapse("agents", "delete", agent["id"], "--yes")
        assert code == errors.ERROR
        assert "Uses it" in err

    async def test_a_team_names_its_members(self, synapse):
        from _fakes import seed as S
        from core.store.resources import get_agent

        ana, ben = S.make_agent(name="Ana"), S.make_agent(name="Ben")
        await S.seed_agents([ana, ben])  # one write: `seed_agent` replaces the collection
        code, out, err = await synapse("agents", "create", "--name", "Crew", "--type", "delegate",
                                       "--members", "ana,Ben", "--prompt", "Lead them.")
        assert code == 0, err
        assert (await get_agent(out.strip()))["delegate_agent_ids"] == [ana["id"], ben["id"]]


# ── workflows ───────────────────────────────────────────────────────────────


class TestWorkflows:
    async def test_exported_made_again_checked_and_deployed(self, synapse, seed_orchestration, tmp_path):
        from core.store.resources import get_orchestration

        source = await seed_orchestration(name="Digest", entry_step_id="s", steps=_WHOLE)
        code, exported, _ = await synapse("workflows", "export", source["id"])
        assert code == 0
        spec = json.loads(exported)
        assert spec["steps"][0]["prompt_template"] == "hi"

        code, out, err = await synapse("workflows", "create", "-f", _file(tmp_path, "w.json", spec),
                                       "--name", "Digest copy")
        assert code == 0, err
        copy = await get_orchestration(out.strip())
        assert copy["name"] == "Digest copy" and copy["id"] != source["id"]
        assert [s["id"] for s in copy["steps"]] == ["s", "end"] and copy["entry_step_id"] == "s"

        code, out, err = await synapse("workflows", "validate", "digest copy")
        assert code == 0 and "ready to run" in out, out + err

        code, out, err = await synapse("workflows", "deploy", copy["id"])
        assert code == 0, err
        from core.store.resources import get_agent
        deployed = await get_agent(out.strip())
        assert deployed is not None and deployed.get("orchestration_id") == copy["id"]
        assert "synapse chat --agent" in err

    async def test_a_workflow_that_would_not_run_is_said_and_exits_1(self, synapse, seed_orchestration):
        orch = await seed_orchestration(name="Broken", entry_step_id="s",
                                        steps=[{"id": "s", "name": "Ask", "type": "agent", "agent_id": "agent_gone",
                                                "prompt_template": "x", "next_step_id": None}])
        code, out, _ = await synapse("workflows", "validate", orch["id"])
        assert code == errors.ERROR
        assert "would not run yet" in out and "✗" in out

    async def test_updated_from_a_file_and_deleted(self, synapse, seed_orchestration, tmp_path):
        from core.store.resources import get_orchestration

        orch = await seed_orchestration(name="Draft", entry_step_id="s", steps=_STEPS)
        steps = [{**_STEPS[0], "prompt_template": "hello"}]
        code, _, err = await synapse("workflows", "update", orch["id"], "-f",
                                     _file(tmp_path, "u.json", {"steps": steps, "description": "v2"}))
        assert code == 0, err
        changed = await get_orchestration(orch["id"])
        assert changed["steps"][0]["prompt_template"] == "hello" and changed["description"] == "v2"

        code, out, _ = await synapse("workflows", "delete", "draft", "--yes")
        assert code == 0 and "Deleted" in out
        assert await get_orchestration(orch["id"]) is None

    async def test_orchestrations_still_names_them(self, synapse, seed_orchestration):
        await seed_orchestration(name="Old name")
        for group in ("orchestrations", "orch", "workflows"):
            code, out, _ = await synapse(group, "list", "-q")
            assert code == 0 and out.strip(), group


# ── what waits, activity, usage ─────────────────────────────────────────────


class TestWhatWaits:
    async def test_each_item_says_how_to_answer_it(self, synapse):
        from core.notifications import hub

        hub.publish("human_input", "CLI1 probe run asks", body="Which client?", run_id="run_cli1_q",
                    data={"step_id": "ask"})
        hub.publish("tool_approval", "CLI1 probe schedule asks", body="send_email",
                    data={"schedule_id": "sched_cli1", "approval_id": "appr_cli1", "tool_name": "send_email"})
        code, out, _ = await synapse("runs", "waiting")
        assert code == 0
        assert "synapse runs answer run_cli1_q" in out
        assert "synapse schedules approve sched_cli1" in out

        code, out, _ = await synapse("runs", "waiting", "--kind", "run_question", "--json")
        mine = [i for i in json.loads(out) if str(i.get("title")).startswith("CLI1 probe")]
        assert [i["kind"] for i in mine] == ["run_question"]


class TestActivityAndUsage:
    async def test_activity_names_a_failed_run(self, synapse, seed_orchestration):
        orch = await seed_orchestration(name="Breaks", entry_step_id="missing", steps=_STEPS)
        await synapse("run", orch["id"])
        code, out, _ = await synapse("activity", "--days", "1")
        assert code == 0
        assert "failed" in out and "Breaks" in out

    async def test_usage_by_model_and_agent(self, synapse, seed_agent):
        from core.usage_tracker import log_usage

        agent = await seed_agent(name="Spender")
        await log_usage(model="gpt-4.1-mini", provider="openai", input_tokens=1000, output_tokens=200, context_chars=0,
                        session_id="s1", agent_id=agent["id"], source="chat")
        await log_usage(model="gpt-4.1-mini", provider="openai", input_tokens=500, output_tokens=100, context_chars=0,
                        session_id="s1", agent_id=agent["id"], source="chat")
        code, out, _ = await synapse("usage", "--by", "model", "--json")
        assert code == 0
        body = json.loads(out)
        row = next(r for r in body["rows"] if r["key"] == "gpt-4.1-mini")
        assert (row["input_tokens"], row["output_tokens"], row["calls"]) == (1500, 300, 2)

        code, out, _ = await synapse("usage", "--by", "agent")
        assert code == 0 and "Spender" in out and "1,500" in out


# ── Boards ──────────────────────────────────────────────────────────────────


@pytest.fixture
async def boards_on(client):
    assert (await client.post("/api/settings", json={"boards_enabled": True})).status_code == 200


class TestBoards:
    async def test_a_board_its_card_closed_and_deleted(self, synapse, boards_on):
        code, out, err = await synapse("boards", "create", "Launch", "--description", "Before the 20th")
        assert code == 0, err

        code, out, err = await synapse("boards", "card", "add", "Launch", "Write the notes", "--body", "Short")
        assert code == 0, err
        card_id = out.strip()
        assert card_id.startswith("card")

        code, out, _ = await synapse("boards", "show", "launch")
        assert code == 0 and "Write the notes" in out and "open" in out

        code, _, err = await synapse("boards", "card", "comment", card_id, "On it")
        assert code == 0, err
        code, _, err = await synapse("boards", "card", "update", card_id, "--status", "done", "--result", "Sent")
        assert code == 0, err
        code, out, _ = await synapse("boards", "show", "Launch", "--all")
        assert "Write the notes" in out and "Sent" in out

        code, out, _ = await synapse("boards", "list", "-q")
        assert code == 0 and out.strip()
        code, out, _ = await synapse("boards", "delete", "Launch", "--yes")
        assert code == 0 and out.strip() == "Deleted."

    async def test_off_says_so(self, synapse):
        code, _, err = await synapse("boards", "list")
        assert code != 0 and "Boards" in err


# ── any tool, any route ─────────────────────────────────────────────────────


class TestEveryDoor:
    async def test_tools_are_listed_shown_and_called(self, synapse, seed_orchestration):
        await seed_orchestration(name="Digest")
        code, out, _ = await synapse("tools", "list")
        assert code == 0 and "list_orchestrations" in out and "get_usage" in out

        code, out, _ = await synapse("tools", "show", "list_runs")
        assert code == 0 and "ARGUMENT" in out and "limit" in out

        code, out, _ = await synapse("tools", "call", "list_orchestrations", "--json")
        assert code == 0 and "Digest" in out

        code, _, err = await synapse("tools", "call", "no_such_tool")
        assert code == errors.NOT_FOUND and "Unknown tool" in err

    async def test_a_delete_through_call_asks_first_too(self, synapse, seed_agent):
        agent = await seed_agent(name="Gone soon")
        code, _, err = await synapse("tools", "call", "delete_agent", "--arg", f"agent_id={agent['id']}")
        assert code == errors.USAGE and "--yes" in err
        code, _, _ = await synapse("tools", "call", "delete_agent", "--arg", f"agent_id={agent['id']}", "--yes")
        assert code == 0

    async def test_any_route(self, synapse, seed_agent, tmp_path):
        agent = await seed_agent(name="Plain")
        code, out, _ = await synapse("api", "GET", "/agents")
        assert code == 0 and agent["id"] in out

        code, out, _ = await synapse("api", "GET", f"/api/v1/agents/{agent['id']}")
        assert code == 0 and json.loads(out)["name"] == "Plain"

        code, out, _ = await synapse("api", "GET", "/orchestrations/runs", "-F", "limit=1")
        assert code == 0 and json.loads(out) == []

        code, out, err = await synapse("api", "GET", "/agents/agent_nope")
        assert code == errors.NOT_FOUND
        assert "not found" in out.lower(), "the body is printed as the server sent it"


class TestWhatACloudCharged:
    async def test_usage_shows_what_was_charged_apart_from_the_estimate(self, synapse):
        from core.product_tools import usage

        async def cloud(since, by):
            return [{"key": "synapse.nova-pro", "cost_usd": 0.0, "charged_usd": 0.0174, "input_tokens": 900,
                     "output_tokens": 80, "calls": 3}]
        usage.set_usage_source(cloud)
        try:
            code, out, err = await synapse("usage", "--by", "model")
        finally:
            usage.set_usage_source(None)
        assert code == 0
        assert "ESTIMATED" in out and "CHARGED" in out and "$0.0174" in out
        assert "$0.0174 charged for Synapse's models" in err
