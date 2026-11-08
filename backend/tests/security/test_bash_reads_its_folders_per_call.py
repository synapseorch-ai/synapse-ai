"""
Allowed Directories saved while the backend runs reach `run_bash` (F241).

`run_bash` is a stdio tool server. It read Settings → Allowed Directories through
`load_settings()`, which in that subprocess is the snapshot its bootstrap took and
never refreshes — F213's class, fixed for the file tools by `8f06063` and not for
bash. Measured by the stress pass: save `bash_allowed_dirs`, run a bash Tool step
there, and the refusal sent you to "Settings → General → Bash Command
Directories", the setting just saved — under a name no screen uses. It ran only
after a restart.

Now the list is `core/file_roots.py`'s, read from the store on every call: the
repositories, the vault, then Allowed Directories — the Filesystem server's list.
Who may run bash at all is unchanged: a widget visitor gets `run_bash` only if the
owner ticked it (F280), and then these same folders.
"""
import json

import pytest

from test_file_tool_confinement import _save_allowed_dirs, layout  # noqa: F401 — fixture


@pytest.fixture
def a_tool_servers_settings(monkeypatch):
    """`load_settings()` as a running tool server has it: the runtime provider,
    serving the snapshot its bootstrap took at start. Its bootstrap is over, so a
    call does not run one again (in a test process the first call would, inline,
    and refresh the snapshot after the save). A save made by the backend — another
    process — never reaches that snapshot. Dropped afterwards, so no later test
    reads it."""
    from core import settings_runtime, tool_server

    async def started():
        return None

    settings_runtime.install_provider()
    monkeypatch.setattr(tool_server, "ready", started)
    yield settings_runtime
    settings_runtime.invalidate()


async def _run(command, cwd=None):
    from tools import bash

    args = {"command": command}
    if cwd is not None:
        args["cwd"] = str(cwd)
    return json.loads((await bash.call_tool("run_bash", args))[0].text)


async def test_a_folder_saved_after_the_tool_started_runs_there(layout, tmp_path, a_tool_servers_settings):
    # The subprocess's state: its bootstrap snapshot, taken before the save below.
    await a_tool_servers_settings.refresh()
    late = tmp_path / "added-later"
    late.mkdir()
    await _save_allowed_dirs([layout["allowed"], late])

    out = await _run("echo ran-here", cwd=late)
    assert "error" not in out, out
    assert out["exit_code"] == 0 and "ran-here" in out["stdout"]


async def test_a_folder_taken_off_the_list_is_refused_at_once(layout, a_tool_servers_settings):
    await a_tool_servers_settings.refresh()
    await _save_allowed_dirs([])

    out = await _run("echo nope", cwd=layout["allowed"])
    assert "not in the allowed list" in out["error"]


async def test_the_refusal_names_the_setting_as_the_screen_does(layout):
    out = await _run("echo nope", cwd=layout["outside"])
    assert "Settings → General → Allowed Directories" in out["error"]
    assert "Bash Command Directories" not in out["error"]
    assert str(layout["outside"]) not in out["allowed_dirs"]


async def test_the_repositories_and_the_vault_are_always_allowed(layout):
    await _save_allowed_dirs([])
    for cwd in (layout["repo"], layout["vault"]):
        out = await _run("echo ok", cwd=cwd)
        assert out.get("exit_code") == 0, out


async def test_with_no_folder_named_it_runs_in_the_first_repository(layout):
    out = await _run("pwd")
    assert out["cwd"] == str(layout["repo"])
