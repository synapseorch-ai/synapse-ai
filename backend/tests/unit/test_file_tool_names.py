"""The file tools an agent is given, and the ones it is told about, exist (F21,
F22, F79 — D104).

Three servers answer to file-reading names. On a fleet only `tools/vault_fs.py`
runs (`list_files`, `read_file`, `search_files`, `read_json`, paths relative to
the vault). The single-process product also runs `tools/file_reader.py`
(`read_file_by_lines`, `grep_file`) and, when `npx` exists, the Node Filesystem
server — which registered after the vault and took the bare `read_file` and
`search_files` from it. The defaults named `grep`, which no server serves, and
left out `list_files` and `read_json`; the prompt told every agent to use `grep`
and `read_file_by_lines` on a vaulted output and never to use `read_file`, which
on a fleet is the one tool that reads it.
"""
from core import tools as T
from core.tool_router import ToolRouter


class TestTheDefaults:
    def test_a_narrowed_agent_still_gets_the_vault_tools(self, monkeypatch):
        monkeypatch.setattr(T, "load_settings", lambda: {})
        allowed = set(T.effective_allowed_tools({"tools": ["time"]}))
        assert {"list_files", "read_file", "search_files", "read_json"} <= allowed
        # The single-process reader's real name, which a vault reference names.
        assert "grep_file" in allowed


class TestWhatThePromptSays:
    def _prompt(self, monkeypatch, inject_tools):
        monkeypatch.setattr(T, "load_settings", lambda: {})
        return T.build_system_prompt(
            "You are an agent.", "[]", "sess", lambda *_a, **_k: {}, None,
            inject_tools=inject_tools,
        )

    def test_the_vault_guidance_names_no_tool_and_forbids_none(self, monkeypatch):
        """Which tools read a vaulted output depends on the product; the
        reference's own `message` names the right ones, so the prompt defers."""
        prompt = self._prompt(monkeypatch, inject_tools=False)
        assert "Do NOT use `read_file`" not in prompt
        assert "use `grep`" not in prompt
        assert "`message`" in prompt

    def test_the_tools_block_names_no_tool_that_does_not_exist(self, monkeypatch):
        prompt = self._prompt(monkeypatch, inject_tools=True)
        assert "**`grep`**" not in prompt and "**`glob`**" not in prompt


class TestTheSingleProcessReference:
    def test_it_names_the_reader_that_exists(self, monkeypatch, tmp_path):
        import json

        from core import vault

        class _Local:
            materialises = False
            root = tmp_path

            def write(self, path, content):
                return None

        monkeypatch.setattr(vault, "get_vault", lambda: _Local())
        monkeypatch.setattr(vault, "_make_vault_path", lambda name, ext: tmp_path / f"{name}.{ext}")
        import core.config as config
        monkeypatch.setattr(config, "load_settings", lambda: {"vault_enabled": True, "vault_threshold": 10})
        message = json.loads(vault.maybe_vault("big_tool", "x" * 5000))["message"]
        assert "grep_file" in message
        assert "grep to" not in message


class TestTheVaultKeepsItsNames:
    def test_filesystem_takes_no_bare_name_the_vault_already_serves(self):
        router = ToolRouter()
        router.register("vault", "read_file")
        router.register("vault", "search_files")
        for name in ("read_file", "search_files", "list_directory"):
            router.register_beside("Filesystem", name)

        assert router.resolve("read_file") == "vault__read_file"
        assert router.resolve("search_files") == "vault__search_files"
        # Still reachable, by its own name, and unclaimed names are its own.
        assert router.resolve("Filesystem__read_file") == "Filesystem__read_file"
        assert router.resolve("list_directory") == "Filesystem__list_directory"
        assert router.ambiguous() == {}

    def test_the_filesystem_server_registers_beside_the_vault(self):
        import inspect

        from core import server
        source = inspect.getsource(server._connect_filesystem_mcp)
        assert 'tool_router.register_beside("Filesystem", tool.name)' in source
