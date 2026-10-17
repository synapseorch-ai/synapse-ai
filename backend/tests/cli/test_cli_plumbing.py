"""The CLI's plumbing: exit codes, errors a script reads, a fast local path, one call per lookup, every run line
shown, a cut stream read on (CLI1, D294, F710 F713 F714 F715 F716).
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from cli_harness import main
from synapse.client import errors, http, target
from synapse.client.commands import RunFollower, _Tracking, _follow
from synapse.client.errors import StreamBroken
from synapse.client.output import Output

REPO = Path(__file__).resolve().parents[3]


# ── F715: exit codes and errors a script reads ──────────────────────────────


class TestExitCodes:
    async def test_a_run_that_does_not_exist_is_5(self, synapse):
        code, out, err = await synapse("runs", "show", "run_nope_1")
        assert code == errors.NOT_FOUND and out == ""
        assert "not found" in err.lower()

    async def test_an_agent_named_nothing_is_5(self, synapse):
        code, _, err = await synapse("agents", "show", "Nobody")
        assert code == errors.NOT_FOUND and "No agent" in err

    async def test_a_refused_key_is_6(self, live_server, monkeypatch, capsys):
        code, _, err = await asyncio.to_thread(main, monkeypatch, capsys, "agents", "list",
                                               "--url", live_server.url, "--api-key", "sk-syn-" + "0" * 32)
        assert code == errors.REFUSED
        assert "Invalid or revoked API key" in err

    def test_nobody_answering_is_7(self, monkeypatch, capsys):
        code, _, err = main(monkeypatch, capsys, "agents", "list", "--url", "http://127.0.0.1:9", "--api-key", "k")
        assert code == errors.UNAVAILABLE and "Could not reach" in err

    async def test_with_json_an_error_is_one_object_on_stderr(self, synapse):
        code, out, err = await synapse("runs", "show", "run_nope_1", "--json")
        assert code == errors.NOT_FOUND and out == ""
        body = json.loads(err.strip().splitlines()[-1])
        assert body["error"]["status"] == 404 and body["error"]["exit_code"] == 5
        assert "not found" in body["error"]["message"].lower()

    def test_a_remote_origin_with_no_key_is_6_and_names_every_way_to_give_one(self, monkeypatch, capsys):
        code, _, err = main(monkeypatch, capsys, "agents", "list", "--url", "https://api.synapseorch.com")
        assert code == errors.REFUSED
        assert "--api-key" in err and "SYNAPSE_API_KEY" in err and "synapse login" in err


def _client(handler) -> http.ApiClient:
    api = http.ApiClient(target.Target(origin="https://x.test", api_base="https://x.test/api/v1", api_key="k",
                                       key_from="--api-key"))
    api._http = httpx.Client(transport=httpx.MockTransport(handler))
    return api


class TestBusy:
    def test_a_short_retry_after_is_waited_out_once(self, monkeypatch):
        slept, calls = [], []
        monkeypatch.setattr(http.time, "sleep", slept.append)

        def handler(request):
            calls.append(1)
            if len(calls) == 1:
                return httpx.Response(429, headers={"Retry-After": "2"}, json={"detail": "Rate limit exceeded."})
            return httpx.Response(200, json=[])
        assert _client(handler).get("/agents") == []
        assert slept == [2.0] and len(calls) == 2

    def test_busy_twice_is_7(self, monkeypatch):
        monkeypatch.setattr(http.time, "sleep", lambda s: None)
        api = _client(lambda r: httpx.Response(503, headers={"Retry-After": "1"}, json={"detail": "At capacity."}))
        with pytest.raises(errors.ApiError) as raised:
            api.get("/agents")
        assert raised.value.code == errors.UNAVAILABLE

    def test_a_long_retry_after_is_not_waited(self, monkeypatch):
        monkeypatch.setattr(http.time, "sleep", lambda s: pytest.fail("waited"))
        api = _client(lambda r: httpx.Response(429, headers={"Retry-After": "600"}, json={"detail": "Later."}))
        with pytest.raises(errors.ApiError) as raised:
            api.get("/agents")
        assert raised.value.code == errors.UNAVAILABLE

    def test_an_empty_answer_is_none_not_a_crash(self):
        """A 204 (an upload cancelled) has no body; `.json()` on it raised before."""
        assert _client(lambda r: httpx.Response(204)).delete("/knowledge/uploads/kup_x") is None


# ── F710: the local path reads four settings, read-only, without the engine ──


class TestTheLocalPath:
    def _store(self, clean_install, values: dict):
        from synapse import _store
        if values:
            _store.save_settings(values)
        return _store

    @pytest.mark.parametrize("values", [
        {},
        {"backend_port": 8899},
        {"login_enabled": True, "login_username": "naveen", "login_password_hash": "$2b$x"},
    ])
    def test_the_peek_reads_what_the_store_reads(self, clean_install, values):
        store = self._store(clean_install, values)
        loaded = store.load_settings()
        peeked = store.peek_settings()
        assert peeked is not None
        for key in store.CLIENT_SETTINGS:
            assert peeked.get(key, loaded.get(key) if key not in values else None) == loaded.get(key), key

    def test_a_store_never_opened_holds_nothing(self, clean_install):
        from synapse import _store
        assert _store.peek_settings() == {}
        assert not (clean_install / "store.db").exists(), "the peek made nothing"

    def test_postgres_is_left_to_the_store(self, monkeypatch):
        from synapse import _store
        monkeypatch.setenv("SYNAPSE_DB_URL", "postgresql+asyncpg://u@h/db")
        assert _store.peek_settings() is None

    def test_a_command_neither_imports_the_engine_nor_writes(self, clean_install):
        """Measured before (F710): 0.41 s a command against 0.16 s with --url, a quarter of a second in
        importing SQLAlchemy and every model and in `create_all` plus the migrations — a write beside a
        running backend, to read four rows."""
        self._store(clean_install, {"backend_port": 9})  # nothing listens there
        db = clean_install / "store.db"
        before = db.stat().st_mtime_ns
        code = ("import sys, synapse.client.target as t; t.resolve(); "
                "print(t.local_port(), 'sqlalchemy' in sys.modules, 'core.store' in sys.modules)")
        env = {"PATH": os.environ["PATH"], "HOME": str(clean_install), "PYTHONPATH": str(REPO),
               "SYNAPSE_DB_URL": f"sqlite+aiosqlite:///{db}", "SYNAPSE_CLI_CONFIG": str(clean_install / "c.json")}
        done = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=60)
        assert done.stdout.split() == ["9", "False", "False"], done.stderr
        assert db.stat().st_mtime_ns == before


# ── F714: an id is one call ─────────────────────────────────────────────────


class TestOneCallPerLookup:
    async def test_an_id_is_read_and_nothing_listed(self, synapse, seed_agent, monkeypatch):
        agent = await seed_agent(name="Research Bot")
        sent = []
        real = http.ApiClient.send

        def counting(self, method, path, **kw):
            sent.append((method, path))
            return real(self, method, path, **kw)
        monkeypatch.setattr(http.ApiClient, "send", counting)

        code, out, _ = await synapse("agents", "show", agent["id"])
        assert code == 0 and "Research Bot" in out
        assert sent == [("GET", f"/agents/{agent['id']}")]

    async def test_a_name_still_resolves(self, synapse, seed_orchestration):
        await seed_orchestration(name="Weekly report")
        code, out, _ = await synapse("workflows", "show", "weekly report")
        assert code == 0 and "Weekly report" in out


# ── F713: every line a run has ──────────────────────────────────────────────


class TestEveryRunLine:
    @pytest.mark.parametrize("event, shown", [
        ({"type": "step_warning", "message": "A card was left open."}, "! A card was left open."),
        ({"type": "member_started", "member_name": "Ana", "task": "CHANGE A\nmore"}, "· Ana: CHANGE A"),
        ({"type": "member_finished", "member_name": "Ana", "status": "completed", "seconds": 2.5},
         "✓ Ana completed  2.5s"),
        ({"type": "member_answered", "member_name": "Ben", "approved": False}, "· Ben: refused"),
        ({"type": "helper_started", "helper_name": "Scout", "task": "look"}, "· Scout: look"),
        ({"type": "helper_finished", "helper_name": "Scout", "status": "failed"}, "✗ Scout failed"),
        ({"type": "parallel_start", "branch_count": 3}, "⇉ 3 branches at once"),
        ({"type": "routing_decision", "decision": "billing"}, "→ billing"),
        ({"type": "switch_decision", "matched_case": "eu"}, "→ eu"),
        ({"type": "evaluator_decision", "tool_name": "route_done"}, "→ done"),
        ({"type": "loop_iteration", "iteration": 2, "total": 5}, "↻ 2 of 5"),
    ])
    def test_each_is_one_line(self, event, shown, capsys):
        """Before, each of these was dropped: a team's members, helpers, warnings and routes never showed."""
        RunFollower(Output()).line(event)
        assert shown in capsys.readouterr().err


# ── F716: a cut stream is read on from the run's events ─────────────────────


class _Api:
    """An ApiClient that hands out scripted streams and remembers what was asked."""

    def __init__(self, *streams):
        self.streams = list(streams)
        self.asked = []

    def stream(self, method, path, body=None, params=None, headers=None):
        self.asked.append(path)
        return self.streams.pop(0)


def _cut(frames):
    yield from frames
    raise StreamBroken("The connection dropped (RemoteProtocolError).")


def _f(event, frame_id=None):
    return {"id": frame_id, "data": event}


class TestACutStream:
    START = {"type": "orchestration_start", "run_id": "run_o_1", "orchestration_id": "o"}
    STEP = {"type": "step_start", "step_name": "Think", "step_type": "llm", "run_id": "run_o_1"}
    LOG = {"type": "_log_prompt", "prompt": "…"}
    DONE = {"type": "step_complete", "step_name": "Think"}
    FINAL = {"type": "final", "response": "All done."}
    END = {"type": "orchestration_complete", "status": "completed"}

    def test_a_run_reads_on_and_shows_nothing_twice(self, capsys):
        out = Output(json_mode=True)
        follower = _Tracking(out, show_questions=False)
        journal = [_f(self.START, "1"), _f(self.STEP, "2"), _f(self.LOG, "3"), _f(self.DONE, "4"),
                   _f(self.FINAL, "5"), _f(self.END, "6"), _f({"type": "stream_complete"})]
        api = _Api(iter(journal))
        outcome = _follow(api, out, follower, _cut([_f(self.START), _f(self.STEP)]))
        assert outcome.status == "completed" and outcome.response == "All done."
        assert api.asked == ["/orchestrations/runs/run_o_1/events/stream"]
        shown = [json.loads(line)["type"] for line in capsys.readouterr().out.splitlines()]
        assert shown == ["orchestration_start", "step_start", "step_complete", "final", "orchestration_complete",
                         "stream_complete"], "each once, and none of v1's held-back _log_ lines"

    def test_an_answer_from_another_shell_says_how_to_read_on(self):
        out = Output(json_mode=True)
        follower = _Tracking(out, show_questions=False)
        follower.run_id = "run_o_1"
        with pytest.raises(StreamBroken, match="synapse runs logs run_o_1 -f"):
            _follow(_Api(), out, follower, _cut([_f(self.STEP)]), owns_run=False)

    async def test_logs_follow_picks_up_after_the_last_id(self, synapse, monkeypatch):
        calls = []

        def stream(self, method, path, body=None, params=None, headers=None):
            calls.append(headers)
            if len(calls) == 1:
                return _cut([_f({"type": "step_start", "step_name": "A"}, "7")])
            return iter([_f({"type": "step_complete", "step_name": "A"}, "8"), _f({"type": "stream_complete"})])
        monkeypatch.setattr(http.ApiClient, "stream", stream)
        code, out, err = await synapse("runs", "logs", "run_o_1", "-f")
        assert code == 0
        assert calls == [None, {"Last-Event-ID": "7"}]
        assert "reading on" in err and "✓ A" in err


class TestACutStreamTwice:
    START = TestACutStream.START
    STEP = TestACutStream.STEP
    DONE = TestACutStream.DONE
    FINAL = TestACutStream.FINAL
    END = TestACutStream.END

    def test_a_second_cut_skips_everything_shown_so_far(self, capsys):
        """Two cuts show nothing twice: the first reads on by counting what the id-less run stream showed, the
        second from the last id the events stream named (the server honours Last-Event-ID, as both products do)."""
        out = Output(json_mode=True)
        follower = _Tracking(out, show_questions=False)
        journal = [_f(self.START, "1"), _f(self.STEP, "2"), _f(self.DONE, "3"), _f(self.FINAL, "4"),
                   _f(self.END, "5"), _f({"type": "stream_complete"})]
        asked = []

        class Journal(_Api):
            def stream(self, method, path, body=None, params=None, headers=None):
                asked.append(headers)
                after = int((headers or {}).get("Last-Event-ID") or 0)
                frames = [f for f in journal if f["id"] is None or int(f["id"]) > after]
                return _cut(frames[:2]) if len(asked) == 1 else iter(frames)

        outcome = _follow(Journal(), out, follower, _cut([_f(self.START), _f(self.STEP)]))
        assert outcome.status == "completed"
        assert asked == [None, {"Last-Event-ID": "2"}], "the last id it had read, skipped or shown"
        shown = [json.loads(line)["type"] for line in capsys.readouterr().out.splitlines()]
        assert shown == ["orchestration_start", "step_start", "step_complete", "final", "orchestration_complete",
                         "stream_complete"]


class TestAnAnswerAWorkingTeamTakes:
    def test_another_shell_reads_on_from_where_the_run_was_when_it_answered(self, monkeypatch, capsys):
        """`runs answer` in a new shell, to a team that takes the answer while it works (TEAM1c): the run never
        paused, so the CLI reads on from the run's events — after the last one there was before the answer,
        so nothing from before it is shown again."""
        from synapse.client import commands

        asked = []

        class Api(_Api):
            def get(self, path, **params):
                asked.append(("GET", path, params))
                return {"events": [{"id": 41, "event": {}}, {"id": 42, "event": {}}], "last_id": 42}

            def stream(self, method, path, body=None, params=None, headers=None):
                asked.append((method, path, headers))
                if method == "POST":
                    return iter([{"id": None, "data": {"status": "answered", "run_id": "run_o_1", "delivered": True}}])
                return iter([_f({"type": "member_finished", "member_name": "Ben", "status": "completed"}, "43"),
                             _f({"type": "orchestration_complete", "status": "completed"}, "44"),
                             _f({"type": "stream_complete"})])

        api = Api()
        start = commands._journal_end(api, "run_o_1")
        frames = api.stream("POST", "/orchestrations/runs/run_o_1/resume/stream", {"response": {"approve": True}})
        code = commands._drive(api, Output(json_mode=True), frames, owns_run=False, run_id="run_o_1", after=start)
        assert code == 0
        assert asked[-1] == ("GET", "/orchestrations/runs/run_o_1/events/stream", {"Last-Event-ID": "42"})
        shown = [json.loads(line).get("type") or json.loads(line).get("status")
                 for line in capsys.readouterr().out.splitlines()]
        assert shown == ["answered", "member_finished", "orchestration_complete", "stream_complete"]


class TestAStreamWithIds:
    def test_a_cut_stream_whose_frames_have_ids_reads_on_from_the_last(self, capsys):
        """Cloud's run streams name each frame's id: read on from it, rather than count what was shown."""
        out = Output(json_mode=True)
        follower = _Tracking(out, show_questions=False)
        start, step = TestACutStream.START, TestACutStream.STEP
        asked = []

        class Api(_Api):
            def stream(self, method, path, body=None, params=None, headers=None):
                asked.append(headers)
                return iter([_f(TestACutStream.END, "1790-3"), _f({"type": "stream_complete"})])

        outcome = _follow(Api(), out, follower, _cut([_f(start, "1790-1"), _f(step, "1790-2")]))
        assert outcome.status == "completed"
        assert asked == [{"Last-Event-ID": "1790-2"}]
        shown = [json.loads(line)["type"] for line in capsys.readouterr().out.splitlines()]
        assert shown == ["orchestration_start", "step_start", "orchestration_complete", "stream_complete"]
