"""A CLI provider's call runs off the server's event loop, ends at its deadline with nothing left running, ends at
once when its run is stopped, and stops itself when the model reaches for a turn of its own (F1320, CAP3's tier 1 P2
prod-02 on 63dfb505; eb 11-04).

One builder call through `cli.claude.claude-haiku-4-5` took 4150 s and ended "ran its own tool (2 turns)": its
300-second `wait_for` never cut it, and a timeout would have killed the CLI alone, never the node processes it starts.
Now the CLI runs in a worker thread, its own process group, with a deadline the thread holds whatever the loop does
(SYNAPSE_LLM_TIMEOUT, never under 300 s); at the deadline, or when the awaiting run is cancelled, the whole group is
killed, so nothing is left running; the words say the model didn't answer within it. On Windows (OSS installs there
too) the tree is killed with `taskkill /T /F`. `cli.claude` runs with `--max-turns 1`, so a model that reaches for a
second turn ends at once, refused as a tool of the CLI's own.

The CLI here is a real process: a script named `claude` first on PATH, which starts a child and both sleep.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import stat
import sys
import time
from pathlib import Path

import pytest

from core import llm_providers as P
from core.llm_providers import LLMError

pytestmark = pytest.mark.skipif(os.name != "posix", reason="the fake CLI is a POSIX script")

SLEEPER = """#!{python}
import os, subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
with open(os.environ["FAKE_CLI_PIDS"], "w") as f:
    f.write(f"{{os.getpid()}} {{child.pid}}")
time.sleep(600)
"""
ANSWER = """#!{python}
import json, sys
print(json.dumps({{"type": "result", "subtype": "success", "is_error": False, "num_turns": 1, "result": "hello"}}))
"""


#: How long a test waits for a call that should end at its own deadline (2 s) or at once: past it, the call is said
#: to be still running, and the test fails rather than waiting the fake CLI's 600 s (CI2, eb 11-04).
BOUND = 10


async def _bounded(call):
    try:
        return await asyncio.wait_for(call, BOUND)
    except asyncio.TimeoutError:
        raise AssertionError(f"still running {BOUND} s on, after its deadline") from None


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except OSError:
        return True
    return state != "Z"  # a zombie has ended; it waits only for its reaper


@pytest.fixture
def fake_cli(monkeypatch, tmp_path):
    """`claude` first on PATH, its script as the test writes it; the pids it starts land in a file."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    pids = tmp_path / "pids"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("FAKE_CLI_PIDS", str(pids))
    monkeypatch.setenv("SYNAPSE_CLI_WORKDIR_ROOT", str(tmp_path / "work"))
    (tmp_path / "work").mkdir()

    def write(script: str) -> Path:
        path = bin_dir / "claude"
        path.write_text(script.format(python=sys.executable))
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
        return pids

    yield write
    # Whatever the fake CLI started is ended here, whatever the code under test left running.
    for pid in (pids.read_text().split() if pids.exists() else []):
        try:
            os.kill(int(pid), signal.SIGKILL)
        except (ProcessLookupError, ValueError):
            pass


async def _pids_of(path: Path) -> list[int]:
    for _ in range(100):
        if path.exists() and path.read_text().strip():
            return [int(p) for p in path.read_text().split()]
        await asyncio.sleep(0.05)
    raise AssertionError("the fake CLI never started")


class TestTheDeadline:
    async def test_it_ends_at_its_deadline_with_nothing_left_running(self, fake_cli):
        pids = fake_cli(SLEEPER)
        started = time.monotonic()
        with pytest.raises(LLMError) as caught:
            await _bounded(P.call_cli_provider("cli.claude.claude-haiku-4-5", "hi", timeout=2))
        assert time.monotonic() - started < 20
        assert "didn't answer within" in str(caught.value), str(caught.value)
        parent, child = [int(p) for p in pids.read_text().split()]
        for _ in range(50):
            if not (_alive(parent) or _alive(child)):
                break
            await asyncio.sleep(0.1)
        assert not _alive(parent) and not _alive(child), "the CLI and what it started are gone"

    async def test_the_loop_serves_while_it_runs(self, fake_cli):
        fake_cli(SLEEPER)
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0.05)

        beat = asyncio.create_task(ticker())
        with pytest.raises(LLMError):
            await _bounded(P.call_cli_provider("cli.claude.claude-haiku-4-5", "hi", timeout=2))
        beat.cancel()
        assert ticks >= 20, f"the loop ran {ticks} times in two seconds"

    async def test_its_answer_comes_back_as_before(self, fake_cli):
        fake_cli(ANSWER)
        text, _, _ = await _bounded(P.call_cli_provider("cli.claude.claude-haiku-4-5", "hi", timeout=30))
        assert text == "hello"

    def test_the_deadline_is_the_llm_timeout_never_under_the_clis_floor(self, monkeypatch):
        from core import config

        deadline = getattr(P, "cli_deadline", None)
        assert deadline is not None, "one deadline for a CLI call, read when it is made"
        monkeypatch.setattr(config, "LLM_REQUEST_TIMEOUT", 120.0)
        assert deadline() == 300
        monkeypatch.setattr(config, "LLM_REQUEST_TIMEOUT", 900.0)
        assert deadline() == 900


class TestAStoppedRunStopsIt:
    async def test_cancelling_the_call_kills_it_at_once(self, fake_cli):
        pids = fake_cli(SLEEPER)
        call = asyncio.create_task(P.call_cli_provider("cli.claude.claude-haiku-4-5", "hi", timeout=120))
        parent, child = await _pids_of(pids)
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await _bounded(call)
        for _ in range(50):
            if not (_alive(parent) or _alive(child)):
                break
            await asyncio.sleep(0.1)
        assert not _alive(parent) and not _alive(child), "stopped, not left to its deadline"


class TestATurnOfItsOwnEndsAtOnce:
    def test_claude_runs_with_one_turn(self, tmp_path):
        from core.providers.cli_invocation import build_cli_invocation

        inv = build_cli_invocation("cli.claude", "claude-haiku-4-5", workdir=tmp_path, system_prompt="s", prompt="p")
        argv = list(inv.argv)
        assert "--max-turns" in argv and argv[argv.index("--max-turns") + 1] == "1", argv

    def test_its_turn_limit_reached_is_a_tool_of_its_own(self):
        said = json.dumps({"type": "result", "subtype": "error_max_turns", "is_error": True, "num_turns": 2})
        with pytest.raises(LLMError) as caught:
            P._read_cli_answer("cli.claude", "cli.claude.claude-haiku-4-5", 1, said, "", None)
        assert "ran its own tool" in str(caught.value), str(caught.value)

    def test_its_own_words_come_after_ours(self):
        # test_cli_auth_detection's pin ("max turns") holds beside this one: the CLI's result text, scrubbed, capped.
        said = json.dumps({"type": "result", "subtype": "error_max_turns", "is_error": True, "num_turns": 2,
                           "result": "Reached max turns sk-live-abcdefghijklmnopqrstuvwx1234"})
        with pytest.raises(LLMError) as caught:
            P._read_cli_answer("cli.claude", "cli.claude.claude-haiku-4-5", 1, said, "", None)
        told = str(caught.value)
        assert "ran its own tool" in told and "(Claude Code said: Reached max turns" in told, told
        assert "sk-live" not in told, told


class TestWindows:
    def test_the_tree_is_killed_with_taskkill(self, monkeypatch):
        kill = getattr(P, "_kill_cli_tree", None)
        assert kill is not None, "one way to end a CLI and what it started"
        ran: list[list[str]] = []

        class _Proc:
            pid, killed = 4242, False

            def poll(self):
                return None

            def kill(self):
                self.killed = True

        monkeypatch.setattr(P.subprocess, "run", lambda argv, **kw: ran.append(list(argv)))
        proc = _Proc()
        kill(proc, posix=False)
        assert ran == [["taskkill", "/PID", "4242", "/T", "/F"]]

        def refused(argv, **kw):
            raise OSError("no taskkill here")

        monkeypatch.setattr(P.subprocess, "run", refused)
        kill(proc, posix=False)
        assert proc.killed, "where taskkill cannot run, the CLI itself is killed, as before"
