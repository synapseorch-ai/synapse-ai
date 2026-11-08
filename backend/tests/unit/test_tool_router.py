"""
The tool router's namespacing and its bare-name aliases.

The router is the engine's dispatch table, and for most of this project's life it
was keyed by the bare tool name. Two servers advertising one name meant the last
to connect won and the other became unreachable, with nothing recording it —
`tests/install/test_mcp_tool_servers.py` carried a hard-coded allowance for the
one instance of it anybody had noticed.

Keys are `{server}__{tool}` now. What these tests pin is that making that change
did not break the two things every caller depends on: a bare name still resolves
the way it always did, and the router is still an ordinary mapping, because a
dozen tests build a server-module stand-in with `tool_router={}`.
"""
import pytest

from core.tool_router import SEP, ToolRouter


def _router(*entries) -> ToolRouter:
    router = ToolRouter()
    for entry in entries:
        router.register(*entry)
    return router


class TestNamespacing:
    def test_a_key_is_server_then_tool(self):
        router = _router(("file_reader", "read_file_by_lines"))

        assert f"file_reader{SEP}read_file_by_lines" in router
        assert router[f"file_reader{SEP}read_file_by_lines"] == (
            "file_reader",
            "read_file_by_lines",
        )

    def test_the_session_key_can_differ_from_the_namespace(self):
        """External MCP registers `{name}__{tool}` against an `ext_mcp_` session."""
        router = _router(("github", "create_issue", "ext_mcp_github"))

        assert router[f"github{SEP}create_issue"] == ("ext_mcp_github", "create_issue")
        assert router.key_for("ext_mcp_github", "create_issue") == f"github{SEP}create_issue"

    def test_iteration_yields_only_namespaced_keys(self):
        """The two removal paths scan `.items()` and delete what they find."""
        router = _router(("Filesystem", "read_file"), ("bash", "run"))

        assert sorted(router) == [f"Filesystem{SEP}read_file", f"bash{SEP}run"]

    def test_a_tool_name_containing_the_separator_still_round_trips(self):
        """The bare name comes from the value, not from splitting the key."""
        router = _router(("srv", f"weird{SEP}name"))

        assert router[f"srv{SEP}weird{SEP}name"] == ("srv", f"weird{SEP}name")
        assert router.resolve(f"weird{SEP}name") == f"srv{SEP}weird{SEP}name"


class TestBareNameAliases:
    def test_a_bare_name_resolves_to_its_only_server(self):
        router = _router(("file_reader", "read_file_by_lines"))

        assert "read_file_by_lines" in router
        assert router["read_file_by_lines"] == ("file_reader", "read_file_by_lines")
        assert router.get("read_file_by_lines") == ("file_reader", "read_file_by_lines")

    def test_get_consults_the_alias_table(self):
        """`dict.get` does not go through `__getitem__`, so it is overridden.

        `core/tools.py` reaches for a bare name with `.get()`; without the
        override every one of those lookups would silently miss.
        """
        router = _router(("bash", "run_command"))

        assert router.get("run_command") is not None
        assert router.get("no_such_tool") is None
        assert router.get("no_such_tool", "fallback") == "fallback"

    def test_a_missing_name_raises_keyerror(self):
        with pytest.raises(KeyError):
            _router(("bash", "run_command"))["nope"]

    def test_the_last_registration_wins_a_collision(self):
        """Same rule as before — the point is that it is now recorded."""
        router = _router(
            ("code_vault_search", "read_file_by_lines"),
            ("file_reader", "read_file_by_lines"),
        )

        assert router["read_file_by_lines"] == ("file_reader", "read_file_by_lines")
        assert router.resolve("read_file_by_lines") == f"file_reader{SEP}read_file_by_lines"

    def test_the_loser_of_a_collision_is_still_reachable(self):
        """This is what retires `_KNOWN_TOOL_NAME_COLLISIONS`."""
        router = _router(
            ("code_vault_search", "read_file_by_lines"),
            ("file_reader", "read_file_by_lines"),
        )

        key = f"code_vault_search{SEP}read_file_by_lines"
        assert key in router
        assert router[key] == ("code_vault_search", "read_file_by_lines")

    def test_a_collision_is_reported(self):
        router = _router(
            ("code_vault_search", "read_file_by_lines"),
            ("file_reader", "read_file_by_lines"),
            ("bash", "run_command"),
        )

        assert router.ambiguous() == {
            "read_file_by_lines": [
                f"code_vault_search{SEP}read_file_by_lines",
                f"file_reader{SEP}read_file_by_lines",
            ]
        }

    def test_an_external_server_cannot_claim_a_bare_name(self):
        """`alias=False` is the anti-shadowing rule, and it is load-bearing.

        Without it, a tenant registering an MCP server that happens to expose
        `read_file` would take over the bare name for that tenant's runs — the
        native Filesystem tool would stop being declared *and* a bare call would
        dispatch to the tenant's own server.
        """
        router = ToolRouter()
        router.register("Filesystem", "read_file")
        router.register("sneaky", "read_file", session_key="ext_mcp_sneaky", alias=False)

        assert router["read_file"] == ("Filesystem", "read_file")
        assert router.declares("Filesystem", "read_file") is True
        assert router.declares("ext_mcp_sneaky", "read_file") is False
        # ...and it is still reachable under its own key.
        assert router[f"sneaky{SEP}read_file"] == ("ext_mcp_sneaky", "read_file")
        # A name only an external server provides has no bare route at all,
        # which is how external MCP has always worked.
        router.register("sneaky", "exfiltrate", session_key="ext_mcp_sneaky", alias=False)
        assert "exfiltrate" not in router

    def test_the_no_alias_rule_survives_a_removal_elsewhere(self):
        router = ToolRouter()
        router.register("Filesystem", "read_file")
        router.register("bash", "run_command")
        router.register("sneaky", "read_file", session_key="ext_mcp_sneaky", alias=False)

        del router[f"bash{SEP}run_command"]  # forces a reindex

        assert router["read_file"] == ("Filesystem", "read_file")

    def test_declares_is_true_only_for_the_server_a_bare_call_reaches(self):
        router = _router(
            ("code_vault_search", "read_file_by_lines"),
            ("file_reader", "read_file_by_lines"),
        )

        assert router.declares("file_reader", "read_file_by_lines") is True
        assert router.declares("code_vault_search", "read_file_by_lines") is False
        assert router.declares("file_reader", "not_a_tool") is False


class TestRemoval:
    def test_deleting_a_key_drops_its_alias(self):
        router = _router(("github", "create_issue", "ext_mcp_github"))

        del router[f"github{SEP}create_issue"]

        assert "create_issue" not in router
        assert router.resolve("create_issue") is None
        assert router.key_for("ext_mcp_github", "create_issue") is None

    def test_removing_the_winner_hands_the_bare_name_to_the_survivor(self):
        """`/api/mcp/servers/{name}` deletes every key for one server.

        Without reindexing, the bare name would keep pointing at a key that no
        longer exists — a `KeyError` at dispatch rather than a graceful miss.
        """
        router = _router(
            ("code_vault_search", "read_file_by_lines"),
            ("file_reader", "read_file_by_lines"),
        )

        del router[f"file_reader{SEP}read_file_by_lines"]

        assert router["read_file_by_lines"] == ("code_vault_search", "read_file_by_lines")
        assert router.ambiguous() == {}

    def test_removing_a_whole_server_the_way_the_routes_do(self):
        router = _router(
            ("github", "create_issue", "ext_mcp_github"),
            ("github", "list_issues", "ext_mcp_github"),
            ("bash", "run_command"),
        )

        for key in [k for k, (session, _) in router.items() if session == "ext_mcp_github"]:
            del router[key]

        assert list(router) == [f"bash{SEP}run_command"]
        assert "create_issue" not in router

    def test_clear_empties_the_alias_table_too(self):
        router = _router(("bash", "run_command"))

        router.clear()

        assert "run_command" not in router
        assert router.ambiguous() == {}


class TestItIsStillAMapping:
    def test_an_empty_router_behaves_like_an_empty_dict(self):
        """Twelve tests build `SimpleNamespace(..., tool_router={})`."""
        router = ToolRouter()

        assert not router
        assert "anything" not in router
        assert router.get("anything") is None
        assert list(router.items()) == []

    def test_wrapping_a_plain_dict_recovers_the_aliases(self):
        """`aggregate_all_tools` normalises whatever it is handed."""
        plain = {
            f"code_vault_search{SEP}read_file_by_lines": ("code_vault_search", "read_file_by_lines"),
            f"file_reader{SEP}read_file_by_lines": ("file_reader", "read_file_by_lines"),
        }

        router = ToolRouter(plain)

        assert router["read_file_by_lines"] == ("file_reader", "read_file_by_lines")
        assert set(router.ambiguous()) == {"read_file_by_lines"}

    def test_a_direct_assignment_still_registers_an_alias(self):
        """Nothing should depend on going through `register()` to stay correct."""
        router = ToolRouter()

        router["srv__thing"] = ("srv", "thing")

        assert router["thing"] == ("srv", "thing")
