"""
What a tool says about itself: MCP annotations, plumbed from the server that
declared them to the cache that trusts them and the model that reads them.

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
        assert h.cacheable is False          # undeclared is not "safe" either

    def test_read_only_implies_not_destructive(self):
        h = tool_hints(VirtualTool("x", "d", {}, _ann(readOnlyHint=True, idempotentHint=True)))
        assert h.declared and h.read_only and h.idempotent
        assert h.destructive is False
        assert h.cacheable is True

    def test_destructive_defaults_true_once_the_server_has_spoken(self):
        """`destructiveHint` is meaningful only when readOnlyHint is false, and
        its spec default is true — so a server that declares a write tool
        without spelling it out still reads as destructive."""
        h = tool_hints(VirtualTool("x", "d", {}, _ann(readOnlyHint=False)))
        assert h.declared and h.destructive is True

    def test_an_explicit_non_destructive_write_is_honoured(self):
        h = tool_hints(VirtualTool("x", "d", {}, _ann(readOnlyHint=False, destructiveHint=False)))
        assert h.declared and h.destructive is False

    def test_open_world_is_not_cacheable_even_when_read_only(self):
        h = tool_hints(VirtualTool("x", "d", {},
                                   _ann(readOnlyHint=True, idempotentHint=True, openWorldHint=True)))
        assert h.read_only and h.open_world
        assert h.cacheable is False      # the page can differ on every fetch

    def test_a_dict_of_annotations_reads_the_same(self):
        """An external server's payload may arrive as a plain dict."""
        h = tool_hints(VirtualTool("x", "d", {}, {"readOnlyHint": True, "idempotentHint": True}))
        assert h.declared and h.read_only and h.cacheable

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
        assert h.read_only and not h.idempotent and not h.cacheable

    async def test_a_write_tool_is_never_cacheable(self):
        from tools.sandbox import list_tools
        declared = {t.name: t for t in await list_tools()}
        assert not tool_hints(declared["vault_write"]).cacheable


class TestTheCacheAsksTheTool:
    def test_a_declared_read_only_tool_is_cacheable_without_being_listed(self):
        from core.cache import tool_cache
        t = VirtualTool("brand_new", "d", {}, _ann(readOnlyHint=True, idempotentHint=True))
        assert "brand_new" not in tool_cache.DETERMINISTIC_TOOLS
        assert tool_cache.is_cacheable("brand_new", t) is True

    def test_a_declared_write_tool_is_not_cacheable_even_if_listed(self):
        from core.cache import tool_cache
        t = VirtualTool("code_search", "d", {}, _ann(readOnlyHint=False))
        assert "code_search" in tool_cache.DETERMINISTIC_TOOLS   # the override says yes
        assert tool_cache.is_cacheable("code_search", t) is False  # the tool says no

    def test_an_undeclared_tool_still_falls_back_to_the_list(self):
        from core.cache import tool_cache
        assert tool_cache.is_cacheable("code_search", VirtualTool("code_search", "d", {})) is True
        assert tool_cache.is_cacheable("whatever", VirtualTool("whatever", "d", {})) is False

    def test_no_declaration_argument_is_the_old_behaviour(self):
        from core.cache import tool_cache
        assert tool_cache.is_cacheable("code_search") is True
        assert tool_cache.is_cacheable("run_bash") is False


class TestWhatTheModelIsTold:
    def test_one_note_per_tool_and_the_riskiest_wins(self):
        d = describe_with_hints("Run a command", tool_hints(
            VirtualTool("x", "d", {}, _ann(readOnlyHint=False, openWorldHint=True))))
        assert d == "Run a command (modifies data — not reversible)"

    def test_read_only_says_so(self):
        d = describe_with_hints("Read a file", tool_hints(
            VirtualTool("x", "d", {}, _ann(readOnlyHint=True, idempotentHint=True))))
        assert d == "Read a file (read-only)"

    def test_an_undeclared_tool_is_described_exactly_as_before(self):
        assert describe_with_hints("Do a thing", tool_hints(VirtualTool("x", "d", {}))) == "Do a thing"

    def test_it_is_byte_stable(self):
        """The tools array sits inside the prompt-cache prefix: a suffix that
        varied per call would invalidate the cache every turn."""
        h = tool_hints(VirtualTool("x", "d", {}, _ann(readOnlyHint=True, idempotentHint=True)))
        assert len({describe_with_hints("Read a file", h) for _ in range(50)}) == 1

    def test_it_does_not_stack_on_repeated_application(self):
        h = tool_hints(VirtualTool("x", "d", {}, _ann(readOnlyHint=True, idempotentHint=True)))
        once = describe_with_hints("Read a file", h)
        assert describe_with_hints(once, h) == once
