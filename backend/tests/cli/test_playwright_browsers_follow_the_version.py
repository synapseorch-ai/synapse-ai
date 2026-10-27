"""`synapse start` installs the browser the installed Playwright drives, not just any browser (F1106; found by ONB3).

Playwright is unpinned (>=1.44,<2), so an upgrade asks for a new browser build. The check used to count any
`chromium-*` folder as enough and never looked for `chromium_headless_shell-*`, which is what headless Chromium runs
on since Playwright 1.49: on Naveen's machine Playwright 1.62 wanted build 1234, the cache held 1208 and 1222, and the
built-in web reader could not start, so Synapse AI's provider lookup said "the web could not be read". Playwright's own
`browsers.json` names the builds it wants; with that unreadable, the Playwright version a browser install last
succeeded for is compared instead.
"""
from __future__ import annotations

import json
import subprocess

import pytest

from synapse import cli

WANTS = {"browsers": [
    {"name": "chromium", "revision": "1234", "installByDefault": True},
    {"name": "chromium-headless-shell", "revision": "1234", "installByDefault": True},
    {"name": "chromium-tip-of-tree", "revision": "1433", "installByDefault": False},
    {"name": "firefox", "revision": "1500", "installByDefault": True},
]}


@pytest.fixture
def browsers(tmp_path, monkeypatch):
    """A browsers folder, the install's own home, and every install recorded rather than run."""
    path = tmp_path / "ms-playwright"
    path.mkdir()
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(path))
    monkeypatch.setenv("SYNAPSE_HOME", str(tmp_path / "home"))
    (path / "mcp-chrome").mkdir()  # the MCP server's browser is not what these tests are about
    wants = tmp_path / "browsers.json"
    wants.write_text(json.dumps(WANTS))
    monkeypatch.setattr(cli, "_playwright_browsers_json", lambda: wants)
    monkeypatch.setattr(cli, "_playwright_version", lambda: "1.62.0")
    ran: list[list[str]] = []

    def run(cmd, **kw):
        ran.append(list(cmd))
        if "playwright" in cmd and "install" in cmd:
            for name in ("chromium-1234", "chromium_headless_shell-1234"):
                (path / name).mkdir(exist_ok=True)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(cli.subprocess, "run", run)
    monkeypatch.setattr(cli, "_save_browsers_path", lambda p: None)
    return path, ran


def _installs(ran):
    return [c for c in ran if "playwright" in c and "install" in c]


def test_a_stale_browser_from_an_older_playwright_is_replaced(browsers):
    path, ran = browsers
    for old in ("chromium-1208", "chromium-1222", "chromium_headless_shell-1222"):
        (path / old).mkdir()
    cli._ensure_playwright_browsers()
    assert _installs(ran), "Playwright 1.62 wants build 1234 and only 1208/1222 were there, yet nothing was installed"
    assert (path / "chromium_headless_shell-1234").is_dir()


def test_the_builds_this_playwright_wants_are_enough(browsers):
    path, ran = browsers
    for name in ("chromium-1234", "chromium_headless_shell-1234"):
        (path / name).mkdir()
    cli._ensure_playwright_browsers()
    assert not _installs(ran), "every build Playwright wants was there, and it installed anyway"


def test_chromium_without_its_headless_shell_is_installed(browsers):
    path, ran = browsers
    (path / "chromium-1234").mkdir()
    cli._ensure_playwright_browsers()
    assert _installs(ran), "headless Chromium runs on chromium_headless_shell-1234, which was missing"


def test_a_failed_install_warns_and_saves_no_version(browsers, monkeypatch, capsys):
    path, ran = browsers

    def failing(cmd, **kw):
        ran.append(list(cmd))
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(cli.subprocess, "run", failing)
    cli._ensure_playwright_browsers()
    assert "Warning" in capsys.readouterr().out
    assert cli._saved_playwright_version() is None, "a failed install was recorded as done"


def test_a_success_saves_the_version_it_installed_for(browsers):
    cli._ensure_playwright_browsers()
    assert cli._saved_playwright_version() == "1.62.0"


def test_with_browsers_json_unreadable_the_saved_version_decides(browsers, monkeypatch):
    path, ran = browsers
    monkeypatch.setattr(cli, "_playwright_browsers_json", lambda: path / "nowhere.json")
    (path / "chromium-1222").mkdir()
    (path / "chromium_headless_shell-1222").mkdir()

    cli._save_playwright_version("1.62.0")
    cli._ensure_playwright_browsers()
    assert not _installs(ran), "the same Playwright as the last good install, and it installed again"

    cli._save_playwright_version("1.55.0")
    cli._ensure_playwright_browsers()
    assert _installs(ran), "Playwright changed since the last good install, and nothing was installed"
