"""The API commands, driven through `main()` as a person or a script runs them (D126).

Two kinds of test, because the CLI is synchronous and opens an event loop of
its own for every store call (`synapse/_store.py`):

- **Against a server** (async): `main()` runs in a worker thread and talks HTTP
  to the real app behind uvicorn on the test's loop, with a real API key —
  exactly how it reaches a remote Synapse. Nothing here touches the store from
  the CLI's side.
- **This machine's own key and login** (sync): no server, the CLI and the store
  alone, the way `test_cli_smoke.py` drives `api-keys`.

Every test points `SYNAPSE_CLI_CONFIG` into its own tmp_path, so none can read
or write the ~/.synapse/cli.json of the person running the suite.
"""
from __future__ import annotations

import asyncio
import builtins
import json
import os
import stat
import sys

import pytest

from _fakes import engine_events as E

cli = pytest.importorskip("synapse.cli", reason="the synapse package is not importable here")
from synapse.client import commands, target  # noqa: E402
from synapse.client.errors import CliError  # noqa: E402

_THINK = [{"id": "s", "name": "Think", "type": "llm",
           "prompt_template": "hi", "output_key": "out", "next_step_id": None}]
_ASK = [
    {"id": "ask", "name": "Ask", "type": "human", "human_prompt": "Ship it?",
     "human_fields": [{"name": "ok", "type": "boolean", "label": "Ship it"},
                      {"name": "note", "type": "string", "label": "Note"}],
     "output_key": "answer", "next_step_id": "s"},
    *_THINK,
]


@pytest.fixture(autouse=True)
def _own_config(tmp_path, monkeypatch):
    monkeypatch.setenv("SYNAPSE_CLI_CONFIG", str(tmp_path / "cli.json"))
    monkeypatch.setenv("SYNAPSE_CHAT_HISTORY", str(tmp_path / "chat_history"))
    for name in ("SYNAPSE_API_URL", "SYNAPSE_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    target._install_settings.cache_clear()
    yield
    target._install_settings.cache_clear()


def _main(monkeypatch, capsys, *argv) -> tuple[int, str, str]:
    monkeypatch.setattr(sys, "argv", ["synapse", *argv])
    try:
        cli.main()
        code = 0
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.fixture
def synapse(live_server, api_key, monkeypatch, capsys):
    """`await synapse("agents", "list")` → (exit code, stdout, stderr), against the live app."""
    async def run(*argv):
        return await asyncio.to_thread(
            _main, monkeypatch, capsys, *argv, "--url", live_server.url, "--api-key", api_key["raw"])
    return run


def _answers(monkeypatch, *replies):
    """A person at the terminal, typing these replies in order."""
    it = iter(replies)
    monkeypatch.setattr(builtins, "input", lambda prompt="": next(it))
    monkeypatch.setattr(commands, "_interactive", lambda: True)


# ── output ──────────────────────────────────────────────────────────────────


class TestJson:
    async def test_json_is_all_stdout_carries(self, synapse, seed_agent, monkeypatch):
        """Every command checked for Node first and warned on stdout, so on a
        machine without it — one that only drives a remote Synapse — `synapse …
        --json | jq` could never parse. Measured before the fix: "Warning: node
        not found" came ahead of `synapse --version`'s own line."""
        monkeypatch.setattr(cli.shutil, "which", lambda name: None)
        agent = await seed_agent()
        code, out, _ = await synapse("agents", "list", "--json")
        assert code == 0
        assert [a["id"] for a in json.loads(out)] == [agent["id"]]

    async def test_a_stream_is_one_event_per_line(self, synapse, seed_orchestration):
        orch = await seed_orchestration()
        code, out, _ = await synapse("run", orch["id"], "--json")
        assert code == 0
        events = [json.loads(line) for line in out.splitlines()]
        assert events[0]["type"] == "orchestration_start"
        assert events[-1] == {"type": "done"}


# ── agents and orchestrations ───────────────────────────────────────────────


class TestDiscovery:
    async def test_names_resolve_in_any_case(self, synapse, seed_agent):
        await seed_agent(name="Research Bot")
        code, out, _ = await synapse("agents", "show", "research bot")
        assert code == 0 and "Research Bot" in out

    async def test_an_unknown_name_is_an_error(self, synapse):
        code, out, err = await synapse("orchestrations", "show", "nope")
        assert code == 1 and out == ""
        assert "No orchestration" in err

    async def test_orch_is_orchestrations(self, synapse, seed_orchestration):
        orch = await seed_orchestration(name="Weekly report")
        code, out, _ = await synapse("orch", "list")
        assert code == 0 and "Weekly report" in out


# ── run ─────────────────────────────────────────────────────────────────────


class TestRun:
    async def test_the_answer_is_stdout_and_the_progress_stderr(self, synapse, fake_llm, seed_orchestration):
        fake_llm.set_default("The report is ready.")
        orch = await seed_orchestration(name="Report", entry_step_id="s", steps=_THINK)
        code, out, err = await synapse("run", "report", "-m", "go")
        assert code == 0
        assert out.strip() == "The report is ready."
        assert "✓ Think" in err

    async def test_inputs_reach_the_run(self, synapse, seed_orchestration):
        from core.orchestration.state import SharedState
        orch = await seed_orchestration()
        code, _, _ = await synapse("run", orch["id"], "-i", "pages=3", "-i", "topic=cats", "--json")
        assert code == 0
        runs = await SharedState.list_runs(limit=1)
        state = (await SharedState.restore(runs[0]["run_id"])).run.shared_state
        assert state["pages"] == 3 and state["topic"] == "cats"

    async def test_a_question_nobody_can_answer_exits_3_then_is_answered(self, synapse, fake_llm,
                                                                        seed_orchestration):
        fake_llm.set_default("Shipped.")
        orch = await seed_orchestration(entry_step_id="ask", steps=_ASK)
        code, _, err = await synapse("run", orch["id"])
        assert code == 3
        assert "synapse runs answer" in err

        run_id = err.split("synapse runs answer ")[1].split()[0]
        code, out, _ = await synapse("runs", "answer", run_id, "--field", "ok=true", "--field", "note=fine")
        assert code == 0
        assert out.strip() == "Shipped."

    async def test_a_person_answers_at_the_prompt(self, synapse, fake_llm, seed_orchestration, monkeypatch):
        from core.orchestration.state import SharedState
        fake_llm.set_default("Shipped.")
        orch = await seed_orchestration(entry_step_id="ask", steps=_ASK)
        _answers(monkeypatch, "y", "looks good")

        code, out, _ = await synapse("run", orch["id"])

        assert code == 0 and out.strip() == "Shipped."
        runs = await SharedState.list_runs(limit=1)
        state = (await SharedState.restore(runs[0]["run_id"])).run.shared_state
        assert state["answer"] == {"ok": True, "note": "looks good"}

    async def test_a_failed_run_exits_4(self, synapse, seed_orchestration):
        orch = await seed_orchestration(entry_step_id="missing", steps=_THINK)
        code, _, err = await synapse("run", orch["id"])
        assert code == 4
        assert "failed" in err.lower()

    async def test_detach_prints_the_run_and_leaves_it_running(self, synapse, seed_orchestration):
        orch = await seed_orchestration()
        code, out, _ = await synapse("run", orch["id"], "--detach")
        assert code == 0
        run_id = out.strip()
        assert run_id.startswith(f"run_{orch['id']}_")
        for _ in range(100):
            code, out, _ = await synapse("runs", "show", run_id, "--json")
            if json.loads(out)["status"] == "completed":
                break
            await asyncio.sleep(0.05)
        assert json.loads(out)["status"] == "completed"


# ── runs ────────────────────────────────────────────────────────────────────


class TestRuns:
    async def test_list_show_logs(self, synapse, seed_orchestration):
        orch = await seed_orchestration(name="Hello")
        await synapse("run", orch["id"])

        code, out, _ = await synapse("runs", "list")
        assert code == 0 and "Hello" in out and "completed" in out

        code, out, _ = await synapse("runs", "list", "--json")
        run_id = json.loads(out)[0]["run_id"]

        code, out, _ = await synapse("runs", "show", run_id)
        assert code == 0 and "completed" in out

        code, out, _ = await synapse("runs", "logs", run_id, "--json")
        types = [json.loads(line)["event"]["type"] for line in out.splitlines()]
        assert "orchestration_complete" in types

        code, out, _ = await synapse("runs", "logs", run_id, "-f", "--json")
        assert json.loads(out.splitlines()[-1])["event"] == {"type": "stream_complete"}

    async def test_logs_read_a_workers_run_whose_ids_are_not_numbers(self, synapse, monkeypatch):
        """A run a scale-mode worker ran is read from its Redis stream: ids like
        `1790-0`, every event on one page. The CLI paged on with `after=1790-0`,
        which v1's integer `after` refuses — so it stops at a short page."""
        import core.routes.orchestrations as orch_routes

        pages = []

        async def events(run_id, request, after=0, limit=5000):
            pages.append(after)
            return {"events": [{"id": "1790-0", "event": {"type": "orchestration_complete",
                                                          "status": "completed"}}],
                    "last_id": "1790-0", "run": None}
        monkeypatch.setattr(orch_routes, "get_run_events", events)

        code, out, _ = await synapse("runs", "logs", "run_on_fleet", "--json")

        assert code == 0, out
        assert pages == [0]
        assert json.loads(out)["id"] == "1790-0"

    async def test_show_on_a_waiting_run_names_its_question_and_exits_3(self, synapse, seed_orchestration):
        orch = await seed_orchestration(entry_step_id="ask", steps=_ASK)
        _, _, err = await synapse("run", orch["id"])
        run_id = err.split("synapse runs answer ")[1].split()[0]

        code, out, _ = await synapse("runs", "show", run_id)
        assert code == 3
        assert "Ship it?" in out and "ok (boolean)" in out

    async def test_cancel(self, synapse, seed_orchestration):
        orch = await seed_orchestration(entry_step_id="ask", steps=_ASK)
        _, _, err = await synapse("run", orch["id"])
        run_id = err.split("synapse runs answer ")[1].split()[0]

        code, out, _ = await synapse("runs", "cancel", run_id)
        assert code == 0 and out.strip() == "Stopped."


# ── schedules ───────────────────────────────────────────────────────────────


@pytest.fixture
def schedule_manager(test_app):
    from core.scheduler import ScheduleManager
    mgr = ScheduleManager()
    mgr._server_module = test_app.state.server_module
    test_app.state.schedule_manager = mgr
    return mgr


class TestSchedules:
    async def test_create_paused_resume_pause_delete(self, synapse, seed_orchestration, schedule_manager):
        await seed_orchestration(name="Weekly report")
        code, out, _ = await synapse("schedules", "create", "--orchestration", "weekly report",
                                     "--cron", "0 9 * * 1", "--prompt", "this week", "--paused", "--json")
        assert code == 0
        made = json.loads(out)
        assert made["enabled"] is False and made["cron_expression"] == "0 9 * * 1"

        code, out, _ = await synapse("schedules", "list")
        assert "Weekly report" in out and "paused" in out

        code, out, _ = await synapse("schedules", "resume", made["id"])
        assert code == 0 and "next run" in out
        code, out, _ = await synapse("schedules", "pause", made["id"])
        assert "paused" in out

        code, _, err = await synapse("schedules", "delete", made["id"])
        assert code == 2, "deleting asks first, and a script cannot answer"
        code, out, _ = await synapse("schedules", "delete", made["id"], "--yes")
        assert code == 0 and out.strip() == "Deleted."

    async def test_every(self, synapse, seed_agent, schedule_manager):
        await seed_agent(name="Digest")
        code, out, _ = await synapse("schedules", "create", "--agent", "Digest", "--every", "2h", "--json")
        made = json.loads(out)
        assert (made["interval_value"], made["interval_unit"]) == (2, "hours")

    async def test_an_interval_it_cannot_read(self, synapse, seed_agent, schedule_manager):
        await seed_agent(name="Digest")
        code, _, err = await synapse("schedules", "create", "--agent", "Digest", "--every", "fortnightly")
        assert code == 1 and "--every" in err


# ── chat ────────────────────────────────────────────────────────────────────

APPROVAL = {
    "type": "human_input_required", "kind": "tool_approval",
    "prompt": "The agent wants to run **send_email** — it acts outside this workspace. Allow it?",
    "tool_name": "send_email", "tool_args": {"to": "a@b.c"}, "category": "external",
    "approval_id": "appr-1", "fields": [],
}


class TestChat:
    async def test_one_message_one_answer(self, synapse, seed_agent, fake_llm):
        await seed_agent()
        fake_llm.set_default("Hello there.")
        code, out, _ = await synapse("chat", "-m", "hi")
        assert code == 0 and out.strip() == "Hello there."

    async def test_an_approval_is_answered_at_the_prompt(self, synapse, seed_agent, monkeypatch):
        import core.react_engine as re
        await seed_agent()
        calls = []

        async def loop(*args, **kwargs):
            calls.append(kwargs.get("resume_state"))
            if len(calls) == 1:
                yield APPROVAL
            else:
                yield {"type": "final", "response": "Sent."}
        monkeypatch.setattr(re, "run_react_loop", loop)
        _answers(monkeypatch, "y")

        code, out, _ = await synapse("chat", "-m", "email them")

        assert code == 0
        assert out.strip().endswith("Sent.")
        assert calls[1] is not None, "the answer resumed the turn from its snapshot"

    async def test_an_approval_nobody_can_answer_exits_3(self, synapse, seed_agent, monkeypatch):
        import core.react_engine as re
        await seed_agent()
        monkeypatch.setattr(re, "run_react_loop", E.gen_from([APPROVAL]))
        code, _, err = await synapse("chat", "-m", "email them")
        assert code == 3 and "--session" in err

    async def test_the_server_names_the_conversation(self, synapse, seed_agent, monkeypatch):
        """Cloud mints every chat session id and 404s one it did not (D124), so
        the first turn sends none and every later turn sends the server's."""
        import core.react_engine as re
        await seed_agent()
        sent = []

        async def loop(request, *args, **kwargs):
            sent.append(request.session_id)
            yield {"type": "final", "response": "ok"}
        monkeypatch.setattr(re, "run_react_loop", loop)

        class Tty:
            def isatty(self):
                return True
        monkeypatch.setattr(sys, "stdin", Tty())
        replies = iter(["one", "two", "/exit"])
        monkeypatch.setattr(builtins, "input", lambda prompt="": next(replies))

        code, _, _ = await synapse("chat")

        assert code == 0
        assert sent[0].startswith("api_"), "the server minted the first id"
        assert sent[1] == sent[0]

    async def test_the_repl(self, synapse, seed_agent, fake_llm, monkeypatch):
        await seed_agent(name="Helper")
        fake_llm.set_default("Hi!")

        class Tty:
            def isatty(self):
                return True
        monkeypatch.setattr(sys, "stdin", Tty())
        replies = iter(["/session", "hello", "/session", "/nope", "/exit"])
        monkeypatch.setattr(builtins, "input", lambda prompt="": next(replies))

        code, out, err = await synapse("chat")

        assert code == 0
        assert "Hi!" in out
        assert "Chatting with Helper" in err
        assert "No conversation yet" in err
        assert "api_" in err, "the conversation's id is the one the server minted"
        assert "There is no /nope" in err


# ── mcp ─────────────────────────────────────────────────────────────────────


@pytest.fixture
async def mcp_endpoint(test_app):
    """The app's MCP endpoint running, as the lifespan starts it (A's `mcp_app`, D125)."""
    from core.mcp_server.endpoint import default_endpoint

    endpoint = default_endpoint()
    started, stop = asyncio.Event(), asyncio.Event()

    async def _serve():
        async with endpoint.serve():
            started.set()
            await stop.wait()

    task = asyncio.create_task(_serve())
    await asyncio.wait_for(started.wait(), 5)
    try:
        yield
    finally:
        stop.set()
        await asyncio.wait_for(task, 5)


class TestMcp:
    async def test_an_mcp_client_reaches_synapse_through_the_cli(self, live_server, api_key, mcp_endpoint,
                                                                 seed_orchestration, tmp_path):
        """`synapse mcp` as Claude Code or Cursor would start it: its own process, stdio."""
        from datetime import timedelta
        from pathlib import Path

        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        repo = Path(__file__).resolve().parents[3]
        await seed_orchestration(id="orch_hello", name="Say hello")
        env = {k: v for k, v in os.environ.items() if not k.startswith("SYNAPSE_")}
        # Never the install's own store, whatever the repo's .env says.
        env.update(PYTHONPATH=str(repo), SYNAPSE_DB_URL=f"sqlite+aiosqlite:///{tmp_path / 'cli.db'}",
                   SYNAPSE_CLI_CONFIG=str(tmp_path / "cli.json"))
        params = StdioServerParameters(
            command=sys.executable, cwd=str(repo), env=env,
            args=["-m", "synapse", "mcp", "--url", live_server.url, "--api-key", api_key["raw"]])
        async with stdio_client(params) as (read, write), \
                ClientSession(read, write, read_timeout_seconds=timedelta(seconds=30)) as session:
            await session.initialize()
            listed = await session.call_tool("list_orchestrations", {})
        assert not listed.isError
        assert "orch_hello" in listed.content[0].text

    async def test_it_never_writes_to_stdout(self, synapse, monkeypatch):
        """stdout is the MCP protocol's; a note there breaks the client."""
        monkeypatch.setitem(sys.modules, "synapse.mcp_bridge", None)
        code, out, err = await synapse("mcp")
        assert code == 1 and out == ""
        assert "bridge" in err


# ── the target: sync, the CLI and the store alone ───────────────────────────


@pytest.fixture
def clean_install(tmp_path, monkeypatch):
    monkeypatch.setenv("SYNAPSE_DB_URL", f"sqlite+aiosqlite:///{tmp_path / 'store.db'}")
    monkeypatch.setenv("SYNAPSE_BLOB_DIR", str(tmp_path / "blobs"))
    monkeypatch.delenv("SYNAPSE_DATA_DIR", raising=False)
    monkeypatch.delenv("SYNAPSE_BACKEND_PORT", raising=False)
    return tmp_path


def _keys():
    from synapse import _store
    from core.api_keys import list_api_keys
    return _store.run(list_api_keys)


class TestTarget:
    def test_the_flag_beats_the_environment(self, monkeypatch):
        monkeypatch.setenv("SYNAPSE_API_URL", "https://env.example")
        monkeypatch.setenv("SYNAPSE_API_KEY", "sk-syn-env")
        t = target.resolve("https://flag.example", "sk-syn-flag")
        assert (t.origin, t.api_base, t.api_key) == ("https://flag.example", "https://flag.example/api/v1",
                                                     "sk-syn-flag")
        t = target.resolve()
        assert (t.origin, t.api_key) == ("https://env.example", "sk-syn-env")

    def test_a_path_is_the_api_base(self):
        t = target.resolve("https://proxy.example/synapse/api/v1/", "k")
        assert t.origin == "https://proxy.example"
        assert t.api_base == "https://proxy.example/synapse/api/v1"

    def test_a_remote_synapse_needs_a_key(self):
        with pytest.raises(CliError, match="needs an API key"):
            target.resolve("https://api.synapseorch.com")

    def test_the_default_is_this_machines_backend(self, clean_install):
        t = target.resolve()
        assert t.origin == "http://127.0.0.1:8765" and t.managed


class TestThisMachinesKey:
    def test_minted_once_named_and_kept_private(self, clean_install):
        t = target.resolve()
        t.ensure_key()
        assert [k["name"] for k in _keys()] == [target.KEY_NAME]

        path = target.config_path()
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        again = target.resolve()
        again.ensure_key()
        assert again.api_key == t.api_key
        assert len(_keys()) == 1

    def test_a_new_one_replaces_the_old(self, clean_install):
        t = target.resolve()
        t.ensure_key()
        first = t.api_key
        t.mint_key()
        assert t.api_key != first
        assert len(_keys()) == 1, "the replaced key is not left behind"

    def test_a_given_key_is_used_and_nothing_minted(self, clean_install):
        t = target.resolve(api_key="sk-syn-given")
        assert not t.managed
        assert _keys() == []


class TestLoginGate:
    @pytest.fixture
    def login_on(self, clean_install, monkeypatch):
        from synapse import _store
        _store.prepare()
        from core.user_auth import hash_password
        monkeypatch.setenv("SYNAPSE_JWT_SECRET", "a-test-secret-long-enough-for-hs256")
        _store.save_settings({"login_enabled": True, "login_username": "naveen",
                              "login_password_hash": hash_password("pw")})
        target._install_settings.cache_clear()

    def test_off_asks_for_nothing(self, clean_install):
        target.resolve().ensure_key()

    def test_on_refuses_until_logged_in(self, login_on):
        from core.user_auth import create_session_token
        t = target.resolve()
        with pytest.raises(CliError, match="synapse login"):
            t.ensure_key()
        assert _keys() == [], "nothing is minted before the login"

        t.save_session(create_session_token("naveen"))
        target.resolve().ensure_key()
        assert len(_keys()) == 1

    def test_a_session_for_someone_else_is_not_one(self, login_on):
        from core.user_auth import create_session_token
        t = target.resolve()
        t.save_session(create_session_token("someone-else"))
        with pytest.raises(CliError):
            target.resolve().ensure_key()
