"""
One `run_bash` no longer holds up every other (F246).

`run_bash` is one stdio tool server for the whole workspace, and it ran each
command with a blocking `subprocess.run` inside its async handler. The server
could do nothing else until the command ended, so every bash call from every
chat, workflow step and sub-agent waited in one line. Measured by the stress
pass: a `date` sent 6 s after another tab's `sleep 25; date` ran 31 ms after that
one ended — a one-second command waited ~20 s.

Now the command runs as an asyncio subprocess, so the server serves the next
call while one runs. A command that times out, or whose call is cancelled, is
killed with everything it started: it is the leader of its own process group.
`subprocess.run` killed only the shell, and a `sleep` the shell had started ran
on after the error was returned.
"""
import asyncio
import json
import subprocess
import time

import pytest

from test_file_tool_confinement import layout  # noqa: F401 — fixture


async def _run(command, **extra):
    from tools import bash

    return json.loads((await bash.call_tool("run_bash", {"command": command, **extra}))[0].text)


def _alive(marker: str) -> bool:
    """Whether a process whose command line holds `marker` is still running."""
    return subprocess.run(["pgrep", "-f", marker], capture_output=True).returncode == 0


def _reap(marker: str) -> None:
    subprocess.run(["pkill", "-f", marker], capture_output=True)


async def _gone(marker: str, within: float = 2.0) -> bool:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if not _alive(marker):
            return True
        await asyncio.sleep(0.05)
    return False


async def test_a_second_command_runs_while_the_first_is_still_running(layout):
    t0 = time.monotonic()
    first = asyncio.create_task(_run("sleep 2; echo first"))
    await asyncio.sleep(0.3)
    second = await _run("echo second")
    second_done = time.monotonic() - t0
    out = await first
    first_done = time.monotonic() - t0

    assert second["exit_code"] == 0 and "second" in second["stdout"]
    assert out["exit_code"] == 0 and "first" in out["stdout"]
    assert first_done >= 2.0
    assert second_done < 1.0, f"the second command finished at {second_done:.2f}s — it waited for the first"


async def test_a_command_that_times_out_is_killed_with_what_it_started(layout):
    marker = "sleep 30.123"
    try:
        t0 = time.monotonic()
        out = await _run(f"{marker}; echo late", timeout=1)
        elapsed = time.monotonic() - t0

        assert "timed out after 1.0s" in out["error"]
        assert elapsed < 5, f"returned after {elapsed:.1f}s"
        assert await _gone(marker), "the shell's child ran on after the timeout"
    finally:
        _reap(marker)


async def test_a_cancelled_call_kills_its_command(layout):
    marker = "sleep 5.456"
    try:
        t0 = time.monotonic()
        call = asyncio.create_task(_run(f"{marker}; echo late"))
        await asyncio.sleep(0.5)
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
        elapsed = time.monotonic() - t0

        assert elapsed < 3, f"the cancel took effect after {elapsed:.1f}s"
        assert await _gone(marker), "the command ran on after its call was cancelled"
    finally:
        _reap(marker)


async def test_a_background_job_that_let_go_of_the_output_is_left_running(layout):
    """Only a timeout or a cancel kills the group. A job the command detached
    from its output — a server started with `… > log &` — outlives the call, as
    it always has."""
    marker = "sleep 30.789"
    try:
        t0 = time.monotonic()
        out = await _run(f"{marker} > /dev/null 2>&1 & echo started")
        assert out["exit_code"] == 0 and "started" in out["stdout"]
        assert time.monotonic() - t0 < 5
        assert _alive(marker)
    finally:
        _reap(marker)


async def test_the_shell_prefix_still_wraps_the_command(layout, monkeypatch):
    """The scratch harness puts a sandbox in front of every command through
    `_SHELL_PREFIX`; the command is still handed to it as its last argument."""
    from tools import bash

    monkeypatch.setattr(bash, "_SHELL_PREFIX",
                        ["/bin/sh", "-c", 'echo WRAPPED; exec /bin/bash -c "$1"', "wrapper"])
    out = await _run("echo inner")
    assert out["exit_code"] == 0
    assert out["stdout"].split() == ["WRAPPED", "inner"]


async def test_output_that_is_not_utf8_is_still_the_output(layout):
    """Bytes that do not decode are replaced. `text=True` decoded strictly, so one
    stray byte turned the whole result into a `codec can't decode` error."""
    out = await _run(r"printf 'bytes: \377\376 then ok\n'")
    assert "error" not in out, out
    assert out["exit_code"] == 0
    assert "then ok" in out["stdout"]
