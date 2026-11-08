"""
What a tool says about itself: MCP annotations, plumbed from the server that
declared them to the policy that trusts them and the model that reads them.

The hints shipped in the SDK and were read nowhere — they survived
`list_tools()` and died the moment a tool was wrapped, so the engine could not
tell a file read from a database write.
"""
import types

import pytest

from core.tools import (
    VirtualTool, describe_with_hints, tool_hints,
)


def _ann(**kw):
    from mcp.types import ToolAnnotations
    return ToolAnnotations(**kw)


class TestReadingTheHints:
    def test_an_undeclared_tool_is_undeclared_not_destructive(self):
        """MCP's own defaults are destructive=True and open_world=True. Reading
        an absent annotation as its default would mark every un-annotated tool
        in existence as dangerous."""
        h = tool_hints(VirtualTool("x", "d", {}))
        assert h.declared is False
        assert h.destructive is False and h.open_world is False

    def test_read_only_implies_not_destructive(self):
        h = tool_hints(VirtualTool("x", "d", {}, _ann(readOnlyHint=True, idempotentHint=True)))
        assert h.declared and h.read_only and h.idempotent
        assert h.destructive is False

    def test_destructive_defaults_true_once_the_server_has_spoken(self):
        """`destructiveHint` is meaningful only when readOnlyHint is false, and
        its spec default is true — so a server that declares a write tool
        without spelling it out still reads as destructive."""
        h = tool_hints(VirtualTool("x", "d", {}, _ann(readOnlyHint=False)))
        assert h.declared and h.destructive is True

    def test_an_explicit_non_destructive_write_is_honoured(self):
        h = tool_hints(VirtualTool("x", "d", {}, _ann(readOnlyHint=False, destructiveHint=False)))
        assert h.declared and h.destructive is False

    def test_open_world_is_read_alongside_read_only(self):
        h = tool_hints(VirtualTool("x", "d", {},
                                   _ann(readOnlyHint=True, idempotentHint=True, openWorldHint=True)))
        assert h.read_only and h.open_world

    def test_a_dict_of_annotations_reads_the_same(self):
        """An external server's payload may arrive as a plain dict."""
        h = tool_hints(VirtualTool("x", "d", {}, {"readOnlyHint": True, "idempotentHint": True}))
        assert h.declared and h.read_only and h.idempotent

    def test_trust_is_carried_not_inferred(self):
        assert tool_hints(VirtualTool("x", "d", {}), trusted=False).trusted is False
        assert tool_hints(VirtualTool("x", "d", {})).trusted is True


class TestOurOwnServersDeclareThem:
    """These are ours, so the spec's "do not trust hints from an untrusted
    server" caveat does not apply — but only if we actually declare them."""

    @pytest.mark.parametrize("module,tool,expect", [
        ("tools.file_reader", "read_file_by_lines", "read_only"),
        ("tools.code_search", "search_codebase", "read_only"),
        ("tools.vault_fs", "read_json", "read_only"),
        ("tools.bash", "run_bash", "destructive"),
        ("tools.sandbox", "vault_delete", "destructive"),
        ("tools.sandbox", "vault_read", "read_only"),
        ("tools.sql_agent", "run_sql_query", "destructive"),
        ("tools.sql_agent", "list_tables", "read_only"),
        ("tools.web_scraper", "scrape_url", "open_world"),
    ])
    async def test_the_declaration_matches_what_the_tool_does(self, module, tool, expect):
        import importlib
        mod = importlib.import_module(module)
        lister = next(fn for name, fn in vars(mod).items() if name == "list_tools")
        declared = {t.name: t for t in await lister()}
        assert tool in declared, f"{module} no longer declares {tool}"
        h = tool_hints(declared[tool])
        assert h.declared, f"{module}:{tool} lost its annotations"
        assert getattr(h, expect) is True, f"{module}:{tool} is not {expect}: {h}"

    async def test_the_clock_is_read_only_but_never_idempotent(self):
        from tools.time import list_tools
        h = tool_hints({t.name: t for t in await list_tools()}["get_datetime"])
        assert h.read_only and not h.idempotent

    async def test_a_write_tool_is_not_read_only(self):
        from tools.sandbox import list_tools
        declared = {t.name: t for t in await list_tools()}
        h = tool_hints(declared["vault_write"])
        assert not h.read_only and not h.idempotent
