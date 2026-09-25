"""An agent's file tools read only where the Filesystem server may (S5, S8).

The roots are the linked repositories, the vault, and Settings → Allowed
Directories — the list `core/server.py` starts the Filesystem MCP server on
(Naveen, 2026-09-25, D128). Before this:

  * `file_reader`'s `read_file_by_lines` and `grep_file` — injected into every
    agent type — opened any path at all;
  * `code_search`'s `read_file_by_lines` did too;
  * `code_search`'s `grep` and `glob` checked the folder, then joined a
    `..`-bearing pattern onto it and globbed out of it;
  * the vault tool (`tools/sandbox.py`) and `GET /api/file` checked with a bare
    `startswith`, so a sibling `vault-old` passed as inside `vault` (S8).

Every root and every outside file here is in a temp directory; the store, the
settings and the vault root are patched at the one place the old code and the
new both read them.
"""
from __future__ import annotations

import json
import os

import pytest
from fastapi import HTTPException

SECRET = "SECRET-TOKEN-9f3a"


@pytest.fixture
def layout(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    vault = tmp_path / "vault"
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    sibling = tmp_path / "vault-old"
    for d in (repo, vault / "tool_outputs", allowed, outside, sibling):
        d.mkdir(parents=True)

    (repo / "main.py").write_text("print('hello')\n")
    (vault / "tool_outputs" / "big_output.json").write_text('{"k": "vaulted"}\n')
    (allowed / "notes.txt").write_text("allowed notes\n")
    (outside / "secret.txt").write_text(f"token={SECRET}\n")
    (sibling / "old.txt").write_text(f"old={SECRET}\n")
    os.symlink(outside / "secret.txt", repo / "link_to_secret.txt")

    # Imported before anything is patched: a module that binds
    # `from core.config import load_settings` at import time would otherwise
    # keep the fake below for the rest of the session.
    import core.routes.settings  # noqa: F401
    import core.server  # noqa: F401
    import tools.code_search  # noqa: F401
    import tools.file_reader  # noqa: F401
    import tools.sandbox  # noqa: F401
    from core.store import collections

    real_load = collections.load

    async def fake_load(name, *a, **k):
        if name == "repos":
            return [{"id": "repo_1", "name": "repo", "path": str(repo)}]
        return await real_load(name, *a, **k)

    monkeypatch.setattr(collections, "load", fake_load)
    monkeypatch.setattr("core.vault._vault_root", lambda: vault)
    monkeypatch.setattr("tools.sandbox._vault_root", lambda: vault)
    monkeypatch.setattr("core.config.load_settings", lambda: {"bash_allowed_dirs": [str(allowed)]})
    return {"repo": repo, "vault": vault, "allowed": allowed, "outside": outside, "sibling": sibling}


def _payload(result) -> str:
    return result[0].text


# ---------------------------------------------------------------------------
# file_reader — read_file_by_lines, grep_file
# ---------------------------------------------------------------------------


class TestFileReader:
    async def _call(self, name, args):
        from tools import file_reader

        return _payload(await file_reader.call_tool(name, args))

    @pytest.mark.parametrize("name, extra", [
        ("read_file_by_lines", {"start_line": 1, "end_line": 5}),
        ("grep_file", {"query": "token"}),
    ])
    @pytest.mark.parametrize("which", ["outside", "symlink", "dotdot", "sibling"])
    async def test_a_path_outside_the_roots_is_refused(self, layout, name, extra, which):
        path = {
            "outside": layout["outside"] / "secret.txt",
            "symlink": layout["repo"] / "link_to_secret.txt",
            "dotdot": layout["repo"] / ".." / "outside" / "secret.txt",
            "sibling": layout["sibling"] / "old.txt",
        }[which]
        text = await self._call(name, {"file_path": str(path), **extra})
        assert SECRET not in text
        assert "Access denied" in text

    @pytest.mark.parametrize("where", ["repo", "vault", "allowed"])
    async def test_a_path_inside_a_root_still_reads(self, layout, where):
        path = {
            "repo": layout["repo"] / "main.py",
            "vault": layout["vault"] / "tool_outputs" / "big_output.json",
            "allowed": layout["allowed"] / "notes.txt",
        }[where]
        text = await self._call("read_file_by_lines", {"file_path": str(path), "start_line": 1, "end_line": 5})
        assert "Access denied" not in text
        assert json.loads(text)["total_lines"] == 1


# ---------------------------------------------------------------------------
# code_search — read_file_by_lines, grep, glob
# ---------------------------------------------------------------------------


class TestCodeSearch:
    async def _call(self, name, args):
        from tools import code_search

        return _payload(await code_search.call_tool(name, args))

    @pytest.mark.parametrize("which", ["outside", "symlink", "relative"])
    async def test_read_file_by_lines_refuses_outside(self, layout, which):
        args = {
            "outside": {"file_path": str(layout["outside"] / "secret.txt")},
            "symlink": {"file_path": str(layout["repo"] / "link_to_secret.txt")},
            "relative": {"file_path": "../outside/secret.txt", "repo_id": "repo_1"},
        }[which]
        text = await self._call("read_file_by_lines", args)
        assert SECRET not in text
        assert "Access denied" in text

    async def test_read_file_by_lines_inside_a_repo_still_reads(self, layout):
        text = await self._call("read_file_by_lines", {"file_path": "main.py", "repo_id": "repo_1"})
        assert "print('hello')" in text

    async def test_grep_on_a_symlinked_file_is_refused(self, layout):
        text = await self._call("grep", {"path": str(layout["repo"] / "link_to_secret.txt"), "pattern": "token"})
        assert SECRET not in text

    @pytest.mark.parametrize("file_pattern", ["../outside/*", "../*/*.txt", "*"])
    async def test_grep_in_a_folder_stays_in_it(self, layout, file_pattern):
        text = await self._call("grep", {"path": str(layout["repo"]), "pattern": "token|old=", "file_pattern": file_pattern})
        assert SECRET not in text

    async def test_grep_in_a_folder_still_finds_what_is_inside(self, layout):
        text = await self._call("grep", {"path": str(layout["repo"]), "pattern": "hello"})
        assert "main.py" in text

    @pytest.mark.parametrize("pattern", ["../outside/*", "../**/*", "**/*"])
    async def test_glob_stays_in_its_folder(self, layout, pattern):
        text = await self._call("glob", {"folder_path": str(layout["repo"]), "pattern": pattern})
        results = json.loads(text)["results"]
        assert not any("secret" in r or "old.txt" in r for r in results), results

    async def test_glob_still_lists_what_is_inside(self, layout):
        text = await self._call("glob", {"folder_path": str(layout["repo"]), "pattern": "*.py"})
        assert json.loads(text)["results"] == ["main.py"]

    async def test_grep_accepts_an_allowed_directory(self, layout):
        """Allowed Directories is one of the roots now, as it is for the Filesystem server."""
        text = await self._call("grep", {"path": str(layout["allowed"]), "pattern": "allowed"})
        assert "notes.txt" in text


# ---------------------------------------------------------------------------
# S8 — a sibling that starts with the vault's name
# ---------------------------------------------------------------------------


class TestVaultSibling:
    def test_the_vault_tool_refuses_a_sibling(self, layout):
        from tools import sandbox

        assert sandbox._safe_path("../vault-old/old.txt") is None
        assert sandbox._resolve(str(layout["sibling"] / "old.txt")) is None

    def test_the_vault_tool_still_resolves_inside(self, layout):
        from tools import sandbox

        inside = layout["vault"] / "tool_outputs" / "big_output.json"
        assert sandbox._safe_path("tool_outputs/big_output.json") == inside.resolve()
        assert sandbox._resolve(str(inside)) == inside.resolve()

    async def test_api_file_refuses_a_sibling(self, layout):
        from core.routes.settings import get_file

        with pytest.raises(HTTPException) as exc:
            await get_file(path=str(layout["sibling"] / "old.txt"))
        assert exc.value.status_code == 403

    async def test_api_file_still_serves_the_vault(self, layout):
        from core.routes.settings import get_file

        response = await get_file(path=str(layout["vault"] / "tool_outputs" / "big_output.json"))
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# One definition — the Filesystem server gets exactly the same list
# ---------------------------------------------------------------------------


async def test_the_filesystem_server_is_started_on_the_same_roots(layout):
    """Repos, then the vault, then Allowed Directories — the file tools' own list."""
    import core.server as server
    from core import file_roots

    spec = await server._filesystem_server_spec()
    assert spec["args"][:2] == ["-y", "@modelcontextprotocol/server-filesystem"]
    assert spec["args"][2:] == [str(layout["repo"]), str(layout["vault"]), str(layout["allowed"])]
    assert spec["args"][2:] == await file_roots.allowed_roots()


async def test_an_allowed_directory_already_covered_is_not_passed_twice(layout, monkeypatch):
    from core import file_roots

    monkeypatch.setattr(
        "core.config.load_settings",
        lambda: {"bash_allowed_dirs": [str(layout["vault"]), str(layout["allowed"]), "/no/such/dir"]},
    )
    assert await file_roots.allowed_roots() == [str(layout["repo"]), str(layout["vault"]), str(layout["allowed"])]


async def test_the_native_server_list_still_builds(layout, monkeypatch):
    """The whole list, not only the Filesystem entry.

    The first cut of this change removed an import the Browser Automation entry
    below it still used; `_build_native_mcp_servers` raised NameError inside the
    lifespan's Filesystem task, and the backend never finished starting. The
    unit tests' app skips the lifespan, so nothing here noticed (F-row: the OSS
    test pass, 2026-09-25).
    """
    import core.server as server

    async def no_google():
        return {}

    monkeypatch.setattr(server, "load_settings", lambda: {"browser_automation_enabled": True})
    monkeypatch.setattr(server, "_get_google_oauth_env", no_google)

    servers = await server._build_native_mcp_servers()
    by_name = {s["name"]: s for s in servers}
    assert by_name["Filesystem"]["args"][2:] == [str(layout["repo"]), str(layout["vault"]), str(layout["allowed"])]
    browser = by_name["Browser Automation"]["args"]
    assert browser[browser.index("--output-dir") + 1] == str(layout["vault"] / "playwright")
