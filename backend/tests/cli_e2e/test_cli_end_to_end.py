"""`synapse` end to end: the CLI as its own process against a scratch Synapse as its own process (CLI1, D294).

What the in-process tests cannot show: the real argument parser, entry point, exit codes and streams over real
sockets, the backend's own lifespan (its MCP endpoint, scheduler and settings), and this machine's own key minted
into a store. Each test reads as what a person, or a script, types.
"""
from __future__ import annotations

import json
import time

import pytest

from synapse.client import errors

pytestmark = pytest.mark.usefixtures("scratch")


def _workflow(tmp_path, name: str, steps: list, entry: str) -> str:
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps({"name": name, "description": f"{name} (e2e)", "entry_step_id": entry,
                                "steps": steps}))
    return str(path)


THINK = [{"id": "think", "name": "Think", "type": "llm", "prompt_template": "Summarise: {user_input}",
          "output_key": "summary", "next_step_id": "end"},
         {"id": "end", "name": "End", "type": "end"}]
ASK = [{"id": "ask", "name": "Approve", "type": "human", "human_prompt": "Ship it?",
        "human_fields_json": json.dumps([{"name": "ok", "type": "boolean", "label": "Ship it"}]),
        "output_key": "answer", "next_step_id": "think"},
       *THINK]


@pytest.fixture(scope="module")
def summarise(scratch, tmp_path_factory):
    made = scratch.cli("workflows", "create", "-f", _workflow(tmp_path_factory.mktemp("w"), "Summarise", THINK,
                                                              "think"))
    assert made.code == 0, made.err
    return made.out.strip()


@pytest.fixture(scope="module")
def approve(scratch, tmp_path_factory):
    made = scratch.cli("workflows", "create", "-f", _workflow(tmp_path_factory.mktemp("w"), "Ship check", ASK, "ask"))
    assert made.code == 0, made.err
    return made.out.strip()


class TestBuildAndRun:
    def test_a_workflow_from_a_file_checks_out_and_runs(self, scratch, summarise):
        checked = scratch.cli("workflows", "validate", "Summarise")
        assert checked.code == 0 and "ready to run" in checked.out, checked.out + checked.err

        ran = scratch.cli("run", "summarise", "-m", "the quarterly numbers")
        assert ran.code == 0, ran.err
        assert ran.out.strip().startswith("Done:"), "the answer is stdout's"
        assert "✓ Think" in ran.err, "the progress is stderr's"

        listed = scratch.cli("runs", "list", "--workflow", "Summarise", "--json")
        assert listed.code == 0 and listed.json()[0]["status"] == "completed"
        run_id = listed.json()[0]["run_id"]
        logs = scratch.cli("runs", "logs", run_id, "--json")
        assert "orchestration_complete" in {json.loads(line)["event"]["type"] for line in logs.out.splitlines()}

    def test_a_run_detached_is_followed_to_its_end(self, scratch, summarise):
        started = scratch.cli("run", summarise, "-m", "later", "--detach")
        assert started.code == 0 and started.out.strip().startswith("run_")
        followed = scratch.cli("runs", "logs", started.out.strip(), "-f")
        assert followed.code == 0 and "Done:" in followed.out

    def test_a_question_waits_exits_3_and_is_answered(self, scratch, approve):
        ran = scratch.cli("run", "Ship check", "-m", "v2")
        assert ran.code == errors.WAITING
        run_id = ran.err.split("synapse runs answer ")[1].split()[0]

        shown = scratch.cli("runs", "show", run_id)
        assert shown.code == errors.WAITING and "Ship it?" in shown.out

        waiting = scratch.cli("runs", "waiting", "--json")
        assert waiting.code == 0 and run_id in {i.get("run_id") for i in waiting.json()}

        answered = scratch.cli("runs", "answer", run_id, "--field", "ok=true")
        assert answered.code == 0, answered.err
        assert answered.out.strip().startswith("Done:")

    def test_a_waiting_run_is_stopped(self, scratch, approve):
        ran = scratch.cli("run", approve)
        run_id = ran.err.split("synapse runs answer ")[1].split()[0]
        stopped = scratch.cli("runs", "cancel", run_id)
        assert stopped.code == 0 and stopped.out.strip() == "Stopped."

    def test_an_agent_made_chats_and_its_workflow_deploys(self, scratch, summarise):
        made = scratch.cli("agents", "create", "--name", "E2E helper", "--prompt", "You help.", "--model", "oaic.fake")
        assert made.code == 0, made.err
        chat = scratch.cli("chat", "--agent", "e2e helper", "-m", "hello there")
        assert chat.code == 0 and chat.out.strip() == "Done: hello there", chat.err

        deployed = scratch.cli("workflows", "deploy", summarise)
        assert deployed.code == 0 and "synapse chat --agent" in deployed.err


class TestSchedules:
    def test_made_changed_paused_run_and_deleted(self, scratch, summarise):
        made = scratch.cli("schedules", "create", "--workflow", "Summarise", "--every", "1d", "--paused", "--json")
        assert made.code == 0, made.err
        sid = made.json()["id"]
        changed = scratch.cli("schedules", "update", sid, "--cron", "0 9 * * 1", "--prompt", "this week", "--json")
        assert changed.code == 0 and changed.json()["cron_expression"] == "0 9 * * 1"
        assert scratch.cli("schedules", "resume", sid).code == 0
        assert scratch.cli("schedules", "pause", sid).code == 0
        triggered = scratch.cli("schedules", "trigger", sid)
        assert triggered.code == 0, triggered.err
        refused = scratch.cli("schedules", "delete", sid)
        assert refused.code == errors.USAGE, "a script is asked first"
        assert scratch.cli("schedules", "delete", sid, "--yes").out.strip() == "Deleted."


class TestKnowledgeAndBoards:
    def test_knowledge_off_then_on(self, scratch):
        off = scratch.cli("knowledge", "bases", "list")
        assert off.code == errors.NOT_FOUND and "switched off" in off.err

        scratch.internal("POST", "/api/settings", {"knowledge_enabled": True})
        try:
            made = scratch.cli("knowledge", "bases", "create", "E2E notes", "--folder", "e2e")
            assert made.code == 0, made.err
            noted = scratch.cli("knowledge", "add-note", "Releases ship on Fridays.", "--base", "E2E notes",
                                "--title", "Release day")
            assert noted.code == 0 and noted.out.strip().startswith("kdoc"), noted.err
            docs = scratch.cli("knowledge", "docs", "list", "--base", "e2e notes", "--json")
            assert docs.code == 0 and len(docs.json()) == 1
            gone = scratch.cli("knowledge", "bases", "delete", "E2E notes", "--yes")
            assert gone.code == 0
        finally:
            scratch.internal("POST", "/api/settings", {"knowledge_enabled": False})

    def test_a_board_used_as_a_person_uses_it(self, scratch):
        scratch.internal("POST", "/api/settings", {"boards_enabled": True})
        try:
            assert scratch.cli("boards", "create", "E2E launch").code == 0
            card = scratch.cli("boards", "card", "add", "E2E launch", "Write the notes")
            assert card.code == 0, card.err
            closed = scratch.cli("boards", "card", "update", card.out.strip(), "--status", "done", "--result", "Sent")
            assert closed.code == 0, closed.err
            assert "Write the notes" in scratch.cli("boards", "show", "E2E launch", "--all").out
            assert scratch.cli("boards", "delete", "E2E launch", "--yes").code == 0
        finally:
            scratch.internal("POST", "/api/settings", {"boards_enabled": False})


class TestTheWorkspace:
    def test_activity_and_usage_count_the_runs_above(self, scratch, summarise):
        scratch.cli("run", summarise, "-m", "count me")
        activity = scratch.cli("activity")
        assert activity.code == 0 and "Runs:" in activity.out and "completed" in activity.out
        usage = scratch.cli("usage", "--by", "model", "--json")
        assert usage.code == 0, usage.err
        assert any(r["calls"] > 0 for r in usage.json()["rows"]), usage.out

    def test_any_tool_and_any_route(self, scratch):
        listed = scratch.cli("tools", "list")
        assert listed.code == 0 and "get_usage" in listed.out
        called = scratch.cli("tools", "call", "list_orchestrations", "--json")
        assert called.code == 0 and "Summarise" in called.out
        route = scratch.cli("api", "GET", "/orchestrations")
        assert route.code == 0 and "Summarise" in route.out


class TestScripts:
    def test_json_is_all_stdout_carries(self, scratch, summarise):
        for argv in (("agents", "list"), ("workflows", "list"), ("runs", "list"), ("schedules", "list")):
            result = scratch.cli(*argv, "--json")
            assert result.code == 0, argv
            json.loads(result.out)
        streamed = scratch.cli("run", summarise, "-m", "lines", "--json")
        assert all(json.loads(line) for line in streamed.out.splitlines())

    def test_quiet_lists_ids_only(self, scratch, summarise):
        ids = scratch.cli("workflows", "list", "-q").out.split()
        assert summarise in ids and all(" " not in i for i in ids)

    @pytest.mark.parametrize("argv, code", [
        (("agents", "show", "nobody-here"), errors.NOT_FOUND),
        (("runs", "show", "run_nope_1"), errors.NOT_FOUND),
        (("runs", "answer", "run_nope_1"), errors.NOT_FOUND),  # the run is read first: no such run beats no answer
        (("runs", "answer"), errors.USAGE),
        (("schedules", "create", "--agent", "x"), errors.USAGE),
    ])
    def test_each_failure_has_its_code(self, scratch, argv, code):
        assert scratch.cli(*argv).code == code

    def test_a_refused_key_and_nobody_there(self, scratch):
        refused = scratch.cli("agents", "list", "--api-key", "sk-syn-" + "0" * 32, key=False)
        assert refused.code == errors.REFUSED
        gone = scratch.cli("agents", "list", "--url", "http://127.0.0.1:9", url=False)
        assert gone.code == errors.UNAVAILABLE

    def test_an_error_with_json_is_one_object(self, scratch):
        failed = scratch.cli("runs", "show", "run_nope_1", "--json")
        assert failed.out == "" and json.loads(failed.err.strip().splitlines()[-1])["error"]["exit_code"] == 5


class TestThisMachine:
    def test_installed_is_enough_a_key_is_minted_into_its_store_once(self, scratch):
        """No --url, no key: the CLI talks to this machine's backend (the port its store or env names) with a
        key of its own, minted into that store (D126) — here the scratch's, never the live one."""
        first = scratch.cli("agents", "list", key=False, url=False)
        assert first.code == 0, first.err
        shown = scratch.cli("target", "--json", key=False, url=False)
        assert shown.json()["key_from"] == "this machine" and shown.json()["reachable"] is True
        keys = scratch.cli("api-keys", "list", key=False, url=False)
        assert keys.out.count("Synapse CLI") == 1

    def test_a_kept_key_is_used_without_saying_it(self, scratch):
        url = f"http://localhost:{scratch.port}"
        kept = scratch.cli("login", "--url", url, "--api-key-stdin", key=False, url=False, stdin=scratch.key + "\n")
        assert kept.code == 0, kept.err
        shown = scratch.cli("target", "--url", url, "--json", key=False, url=False)
        assert shown.json()["key_from"] == "kept"
        assert scratch.cli("logout", "--url", url, key=False, url=False).code == 0

    def test_a_local_command_is_quick(self, scratch):
        """F710: the local path no longer imports the engine or writes to the store to find its port."""
        scratch.cli("agents", "list", key=False, url=False)  # the key is minted and kept
        took = []
        for _ in range(3):
            t = time.monotonic()
            assert scratch.cli("agents", "list", key=False, url=False).code == 0
            took.append(time.monotonic() - t)
        assert min(took) < 1.5, took


class TestHelp:
    @pytest.mark.parametrize("group", ["agents", "workflows", "runs", "schedules", "knowledge", "boards", "tools"])
    def test_each_group_lists_its_commands(self, scratch, group):
        shown = scratch.cli(group, "--help", key=False, url=False)
        assert shown.code == 0 and "\ncommands:\n" in shown.out and "\n  COMMAND\n" not in shown.out

    def test_the_top_lists_commands_in_sections_and_where_to_start(self, scratch):
        shown = scratch.cli("--help", key=False, url=False)
        assert shown.code == 0
        for heading in ("This machine's Synapse:", "Build:", "Run, answer and schedule:", "start here:",
                        "exit codes:"):
            assert heading in shown.out, heading
        assert "Other:" not in shown.out, "every command has its section"
