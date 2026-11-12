"""Which builder tools Synapse serves over MCP, and how each one is marked (D125).

Since MCP2 (D292) the list and the marks live in `core.product_tools.build`, where each served builder tool is a
product tool with a role, a scope and an audit action; this module keeps the names the server, cloud and the tests
read, derived from there.

An **allowlist**, deliberately. A builder tool added later is not exposed to every
MCP client by accident: it is served only once someone has decided it belongs here.

**Deletes** (`delete_agent`, `delete_orchestration`, `delete_custom_tool`,
`delete_schedule`) are served since D292 (Naveen): irreversible product tools,
previewed first and done only with the confirmation token that preview gave, so a
client asks its person between the two (until then they stayed in the app, because
the client's model would have been the only thing deciding).

Two kinds of builder tool are not served:

* **`finish_build`** cleans up after the builder run it is called from, and an
  MCP call is not one — it would do nothing.
* **`list_variables`** names the variables for the builder's own plans (F1271);
  it stays in the app, beside the screen where they are made.

`list_recent_runs` and `get_run_summary` are left out because the run tools in
`runs.py` answer the same questions with more.

`list_knowledge_bases` is served **only while Knowledge is on** (R1c, D173): a client
writing a Retrieve or Ingest step needs the bases' ids, and while Knowledge is off the
tool does not exist anywhere else either (`builder_tool_offered`). The listing is
decided per call, inside the caller's binding, so cloud answers per organisation.
`search_knowledge` (`knowledge.py`, D176) is not a builder tool but is served the same way.

MCP clients treat a tool as destructive unless it says otherwise, so every tool
here says so explicitly. The overwriters are marked destructive — they replace
what is there — and clients such as Claude Code ask before calling them.
"""
from __future__ import annotations

from mcp import types

from core.product_tools import build as _build

#: Tools that only read (`core.product_tools.build`, where they are product tools now).
READ_TOOLS: tuple[str, ...] = _build.READS

#: Tools that add or edit.
BUILD_TOOLS: tuple[str, ...] = tuple(_build.ACTS)

#: Build tools that only add to what is there; every other one is marked destructive (F1548).
ADDITIVE_TOOLS: frozenset[str] = _build.ADDITIVE

#: Deletes, irreversible: previewed, then done with a confirmation token (D292).
DELETE_TOOLS: tuple[str, ...] = tuple(_build.DELETES)

SERVED_BUILDER_TOOLS: frozenset[str] = frozenset(READ_TOOLS + BUILD_TOOLS + DELETE_TOOLS)

#: What a caller must have been granted to see and call a tool (D127). An embedder
#: may grant less than everything — cloud's OAuth tokens carry these — while a key,
#: and every self-hosted caller, is granted all three. `run` spends model credits.
SCOPES: tuple[str, ...] = ("read", "run", "build")

#: The run tools that only read a run.
READING_RUN_TOOLS: frozenset[str] = frozenset({"get_run", "list_runs"})

#: Knowledge's search (`knowledge.py`, D176): a read that is not a builder tool.
KNOWLEDGE_TOOLS: frozenset[str] = frozenset({"search_knowledge"})


def _entry(name: str):
    from core.product_tools import areas, registry
    areas.ensure_loaded()
    return registry.get(name)


def as_mcp_annotations(tool) -> types.ToolAnnotations:
    """The hints, and its title as well: a client reads the tool's own `title` or this one (F1546)."""
    return types.ToolAnnotations(title=tool.title, **tool.annotations())


def annotations_for(name: str) -> types.ToolAnnotations:
    tool = _entry(name)
    if tool is None:
        raise KeyError(name)
    return as_mcp_annotations(tool)


def scope_of(name: str) -> str | None:
    """The scope a served tool needs; None for a name that is not served."""
    from core.mcp_server import workflows

    tool = _entry(name)
    if tool is not None and not tool.app_only:
        return tool.scope
    if name.startswith(workflows.PREFIX):
        return "run"
    return None


def builder_tools() -> list[types.Tool]:
    """The served builder tools that are listed here right now, as MCP tools, in the allowlist's order.

    A tool `builder_tool_offered` holds back (Knowledge's while it is off) is left out of the listing; a
    call to it still reaches its handler, which refuses."""
    from core.product_tools import areas, registry

    areas.ensure_loaded()
    tools = []
    for name in READ_TOOLS + BUILD_TOOLS + DELETE_TOOLS:
        tool = registry.get(name)
        if tool.offered is not None and not tool.offered():
            continue
        tools.append(types.Tool(name=name, title=tool.title, description=tool.description,
                                inputSchema=tool.schema(confirm_token=tool.needs_token("irreversible")),
                                annotations=as_mcp_annotations(tool)))
    return tools
