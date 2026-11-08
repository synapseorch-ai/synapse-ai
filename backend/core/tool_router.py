"""
Which server services a tool call.

The router is the engine's dispatch table: a name the model produced, mapped to
the MCP session that answers it. It used to be a plain dict keyed by the **bare**
tool name, and that had one silent failure mode. Two servers advertising the same
tool name — `read_file_by_lines` from both `file_reader` and `code_vault_search`,
say — meant the last one to connect won and the other became unreachable, with
nothing anywhere saying so. External MCP servers were namespaced as
``{server}__{tool}`` (`core/server.py`) but native ones were not, so the rule
depended on which branch of startup a server happened to come through.

Now every key is ``{server}__{tool}``, and a bare name is an **alias** that
resolves to whichever server registered it last. Same behaviour as before at the
point of dispatch, with three differences that matter:

* the shadowed copy is still addressable, under its own namespaced key, so an
  agent that wants `code_vault_search`'s repo-aware reader can ask for it;
* the collision is *recorded* rather than lost — see ``ambiguous()``;
* a tenant's own MCP server can no longer quietly take over a native tool name
  for that tenant's runs, because the native tool keeps its own key.

Why a ``dict`` subclass
-----------------------
Because roughly a dozen tests build a server-module stand-in as
``types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})``,
and `core/orchestration/steps.py` reads it as
``getattr(server_module, "tool_router", {})``. Every consumer uses plain mapping
operations — ``in``, ``[]``, ``.get()``, ``.items()``, ``del`` — so staying a
mapping keeps all of them working without a coordinated edit, and a plain ``{}``
remains a valid (if alias-less) router.
"""
from __future__ import annotations

#: Separates the server namespace from the tool name in a router key.
SEP = "__"


class ToolRouter(dict):
    """``{server}__{tool}`` -> ``(session_key, actual_tool_name)``.

    ``session_key`` is the key into ``agent_sessions``, which is not always the
    namespace: external MCP servers register their session under
    ``ext_mcp_{name}`` while their tools are namespaced ``{name}__{tool}``.
    """

    def __init__(self, *args, **kwargs):
        self._alias: dict[str, str] = {}
        self._ambiguous: dict[str, list[str]] = {}
        self._by_session: dict[tuple[str, str], str] = {}
        self._no_alias: set[str] = set()
        super().__init__(*args, **kwargs)
        self._reindex()

    # ── writing ──────────────────────────────────────────────────────────────

    def register(
        self,
        namespace: str,
        tool_name: str,
        session_key: str | None = None,
        alias: bool = True,
    ) -> str:
        """Register `tool_name` as served by `namespace`. Returns the key used.

        The only writer. `session_key` defaults to `namespace`, which is right
        for every native server; external MCP passes its `ext_mcp_` session key
        explicitly.

        `alias=False` registers the tool as reachable **only** by its namespaced
        key. External MCP servers use it, because they have never been callable
        by a bare name — and letting one claim a bare alias is precisely how a
        tenant's own MCP server would take over `read_file` for that tenant's
        runs.
        """
        key = f"{namespace}{SEP}{tool_name}"
        if alias:
            self._no_alias.discard(key)
        else:
            self._no_alias.add(key)
        self[key] = (session_key or namespace, tool_name)
        return key

    def register_beside(self, namespace: str, tool_name: str) -> str:
        """Register `tool_name`, taking its bare name only if no server has it.

        For a server whose tool names collide with a native one's by design. The
        Node Filesystem server answers to `read_file` and `search_files`, which
        are the vault tool's (`tools/vault_fs.py`); it connected later and, last
        registration winning, took them — so on the single-process product an
        agent's `read_file` of a vault path went to a server that reads absolute
        paths (F79). Its tools stay reachable by their namespaced key.
        """
        return self.register(namespace, tool_name, alias=self.resolve(tool_name) is None)

    def __setitem__(self, key, value) -> None:
        super().__setitem__(key, value)
        # The bare name is the second element of the value tuple at every
        # registration site in the tree, so it is read from there rather than by
        # splitting the key — a server whose name contains the separator would
        # make that split wrong, and nothing prevents one.
        bare = value[1] if isinstance(value, tuple) and len(value) == 2 else key
        self._by_session[(value[0], bare)] = key
        if key not in self._no_alias:
            self._note_alias(bare, key)

    def __delitem__(self, key) -> None:
        super().__delitem__(key)
        self._no_alias.discard(key)
        self._reindex()

    def pop(self, key, *default):
        try:
            value = super().pop(key, *default)
        finally:
            self._reindex()
        return value

    def clear(self) -> None:
        super().clear()
        self._reindex()

    # ── reading ──────────────────────────────────────────────────────────────

    def __contains__(self, name) -> bool:
        return super().__contains__(name) or name in self._alias

    def __getitem__(self, name):
        if super().__contains__(name):
            return super().__getitem__(name)
        key = self._alias.get(name)
        if key is None:
            raise KeyError(name)
        return super().__getitem__(key)

    def get(self, name, default=None):
        # dict.get does not consult __getitem__, so the alias fallback has to be
        # spelled out here or every `.get(bare_name)` in the tree silently misses.
        try:
            return self[name]
        except KeyError:
            return default

    def resolve(self, name) -> str | None:
        """The namespaced key `name` dispatches to, bare or already namespaced."""
        if super().__contains__(name):
            return name
        return self._alias.get(name)

    def key_for(self, session_key: str, tool_name: str) -> str | None:
        """The key under which `session_key` registered `tool_name`."""
        return self._by_session.get((session_key, tool_name))

    def declares(self, session_key: str, tool_name: str) -> bool:
        """Whether this session is the one a *bare* `tool_name` dispatches to."""
        key = self._by_session.get((session_key, tool_name))
        return key is not None and self._alias.get(tool_name) == key

    def ambiguous(self) -> dict[str, list[str]]:
        """Bare names more than one server answered, and who answered them.

        Empty in a healthy deployment. Not an error — the last registration wins,
        exactly as it always did — but a collision is now inspectable instead of
        being a tool that mysteriously behaves like a different tool.
        """
        return {bare: list(keys) for bare, keys in self._ambiguous.items()}

    # ── internals ────────────────────────────────────────────────────────────

    def _note_alias(self, bare: str, key: str) -> None:
        previous = self._alias.get(bare)
        if previous is not None and previous != key:
            seen = self._ambiguous.setdefault(bare, [previous])
            for candidate in (previous, key):
                if candidate not in seen:
                    seen.append(candidate)
        self._alias[bare] = key

    def _reindex(self) -> None:
        """Rebuild the alias tables from the surviving entries.

        Called after any removal. Insertion order is preserved by dict, so
        replaying it reproduces the same last-one-wins winners — and a bare name
        whose winner was just deleted falls back to whoever else registered it
        rather than aliasing a key that is gone.
        """
        self._alias = {}
        self._ambiguous = {}
        self._by_session = {}
        for key, value in super().items():
            bare = value[1] if isinstance(value, tuple) and len(value) == 2 else key
            self._by_session[(value[0], bare)] = key
            if key not in self._no_alias:
                self._note_alias(bare, key)
