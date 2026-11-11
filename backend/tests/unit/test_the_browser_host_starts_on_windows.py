"""The browser host starts on Windows, and a host that does not start says what it said (F1533, BRW5; eb 11-11).

A fresh Windows install (2.0.3, the irm script) answered "Keep me signed in" and "Record a task" with a bare 500. The
backend started the browser host with an environment cut to PATH, HOME, LANG and TMPDIR: on Windows that is none of
what a process needs to start, and Playwright's Node driver exited at once ("Connection closed while reading from the
driver"). The host's own words went to DEVNULL, and the backend guessed: "Is Chromium installed?" It was. On this
laptop's Windows side the same host, given SYSTEMROOT and the rest, started and opened a session (BRW5's probe).

Here, on any OS: the environment the backend gives the host, and the one the host gives Chromium and Playwright MCP,
keep what Windows needs and still no secret; a host that stops before it is ready is a `BrowserUnavailable` whose
words are its own last line, also in the backend's log; a start cut short (the engine's 5 s ping) leaves nothing
half-started; and an agent's browser call and its ping get those words.
"""
from __future__ import annotations

import asyncio
import logging
import sys

import pytest

from core.browser import client as browser_client
from core.browser import host as browser_host

#: What Windows needs to start a process: BRW5's probe B added exactly these, and the host started.
WINDOWS = {
    "SYSTEMROOT": r"C:\Windows", "SYSTEMDRIVE": "C:", "WINDIR": r"C:\Windows",
    "COMSPEC": r"C:\Windows\system32\cmd.exe", "PATHEXT": ".COM;.EXE;.BAT;.CMD",
    "USERPROFILE": r"C:\Users\someone", "APPDATA": r"C:\Users\someone\AppData\Roaming",
    "LOCALAPPDATA": r"C:\Users\someone\AppData\Local", "PROGRAMDATA": r"C:\ProgramData",
    "PROGRAMFILES": r"C:\Program Files", "NUMBER_OF_PROCESSORS": "8", "PROCESSOR_ARCHITECTURE": "AMD64",
}
SECRETS = ("OPENAI_API_KEY", "SYNAPSE_INTERNAL_TOKEN", "DATABASE_URL", "SESSION_SECRET", "AWS_SECRET_ACCESS_KEY")

#: The Windows host's last words, as BRW5 read them with the host's stderr kept.
DRIVER_GONE = "Exception: Connection.init: Connection closed while reading from the driver"
FAILING_HOST = ("import sys\n"
                "sys.stderr.write('Traceback (most recent call last):\\n'\n"
                "                 '  File \"host.py\", line 1891, in start_host\\n'\n"
                f"                 '{DRIVER_GONE}\\n')\n"
                "sys.exit(1)\n")


@pytest.fixture
def windows_environ(monkeypatch, tmp_path):
    want = {**WINDOWS, "TEMP": str(tmp_path), "TMP": str(tmp_path)}
    for name, value in want.items():
        monkeypatch.setenv(name, value)
    for name in SECRETS:
        monkeypatch.setenv(name, f"secret-{name.lower()}")
    return want


@pytest.fixture
def host_runs(monkeypatch):
    """The browser host is `python -c CODE` instead of `-m core.browser.host`, started with everything else as the
    backend passes it (its env, its pipes). Returns the processes started."""
    real = asyncio.create_subprocess_exec
    started: list[asyncio.subprocess.Process] = []

    def use(code: str):
        async def fake(program, *args, **kwargs):
            proc = await real(sys.executable, "-c", code, **kwargs)
            started.append(proc)
            return proc
        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)
        return started

    yield use
    for proc in started:
        if proc.returncode is None:
            proc.kill()


class TestTheHostsEnvironmentStartsOnWindows:
    def test_the_backend_gives_the_host_what_windows_needs_and_no_secret(self, windows_environ):
        env = browser_client._Host()._env()
        assert {k: env.get(k) for k in windows_environ} == windows_environ
        assert not set(SECRETS) & set(env)

    def test_the_host_gives_chromium_and_playwright_mcp_the_same(self, windows_environ):
        env = browser_host._clean_env()
        assert {k: env.get(k) for k in windows_environ} == windows_environ
        assert not set(SECRETS) & set(env)


class TestAHostThatDoesNotStartSaysWhy:
    async def test_its_own_last_line_is_the_reason_and_it_is_logged(self, host_runs, caplog):
        host_runs(FAILING_HOST)
        caplog.set_level(logging.WARNING, logger="core.browser.client")
        host = browser_client._Host()
        with pytest.raises(RuntimeError) as failed:
            await host.ensure()
        words = str(failed.value)
        assert type(failed.value).__name__ == "BrowserUnavailable"
        assert DRIVER_GONE in words and "exit code 1" in words
        assert "Is Chromium installed" not in words
        assert DRIVER_GONE in caplog.text
        assert not host.running

    async def test_a_host_that_says_nothing_says_so(self, host_runs):
        host_runs("import sys; sys.exit(3)")
        with pytest.raises(RuntimeError) as failed:
            await browser_client._Host().ensure()
        assert "exit code 3" in str(failed.value) and "Is Chromium installed" not in str(failed.value)

    async def test_a_start_cut_short_leaves_nothing_running(self, host_runs):
        started = host_runs("import time; time.sleep(30)")
        host = browser_client._Host()
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(host.ensure(), 1)
        assert not host.running
        assert started and await asyncio.wait_for(started[0].wait(), 5) is not None


class TestAnAgentsBrowserCallGetsTheWords:
    async def test_a_call_and_its_ping_say_what_the_host_said(self, host_runs):
        host_runs(FAILING_HOST)
        tools = browser_client.BrowserTools(browser_client._Host())
        with pytest.raises(RuntimeError) as pinged:
            await tools.send_ping()
        assert DRIVER_GONE in str(pinged.value)
        token = browser_client.owner.set("chat:f1533")
        try:
            result = await tools.call_tool("browser_navigate", {"url": "https://example.com"})
        finally:
            browser_client.owner.reset(token)
        text = result.content[0].text
        assert result.isError and DRIVER_GONE in text and "Is Chromium installed" not in text
