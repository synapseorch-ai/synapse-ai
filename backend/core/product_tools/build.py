"""The AI builder's own tools, as product tools (MCP2, D292).

Wrapped, not moved: `core/builder_tools.py` (BWA1's) stays their code, and each entry here calls its dispatch, as
the MCP server always did (D125). What the entry adds is what the builder never needed inside its own run: a
role (a read is anyone's, an edit a builder's), the `build` scope, and an audit row for each edit.

The assistant does not take these through the registry: they already are its builder tools (area `build`).

Which builder tools are served, and which are not, is still a decision per tool:

* **Deletes** are served from MCP2c, irreversible, behind a confirmation token (Naveen, D292).
* **`finish_build`** cleans up after the builder run it is called from; a call from outside one does nothing.
* **`list_variables`** answers with the first characters of each value, which are often tokens.
* **`list_recent_runs` / `get_run_summary`**: the run entries answer the same with more.
* **The builder's research and help tools** belong to its own run.
"""
from __future__ import annotations

import contextlib
import contextvars
import json
from typing import Any, Iterator

from core.product_tools.errors import ToolRefusal
from core.product_tools.registry import ProductTool
from core.product_tools.roles import Role

#: Builder tools that only read. `validate_orchestration` clears a build-in-progress mark when an orchestration
#: passes, which changes nothing a person can see.
READS: tuple[str, ...] = (
    "list_agents",
    "get_agent",
    "list_orchestrations",
    "get_orchestration",
    "validate_orchestration",
    "list_tool_servers",
    "list_server_tools",
    "list_all_tools",
    "get_tools_detail",
    "list_custom_tools",
    "list_skills",
    "get_skill",
    "list_schedules",
    "list_repos",
    "list_db_configs",
    "list_knowledge_bases",
)

#: Builder tools that add or edit, with the host's audit action for each.
ACTS: dict[str, str] = {
    "create_agent": "agent.created",
    "create_agents": "agent.created",
    "update_agent": "agent.updated",
    "create_orchestration": "orchestration.created",
    "create_orchestration_skeleton": "orchestration.created",
    "add_steps": "orchestration.updated",
    "add_single_step": "orchestration.updated",
    "add_multiple_steps": "orchestration.updated",
    "update_step": "orchestration.updated",
    "remove_step": "orchestration.updated",
    "update_orchestration": "orchestration.updated",
    "set_orchestration_meta": "orchestration.updated",
    "deploy_orchestration": "orchestration.deployed",
    "create_http_tool": "tool.created",
    "import_openapi": "tool.created",
    "create_skill": "skill.created",
    "update_skill": "skill.updated",
    "attach_skills": "agent.updated",
    "create_schedule": "schedule.created",
    "update_schedule": "schedule.updated",
    "set_schedule_enabled": "schedule.updated",
}

#: Acts that replace or remove what is already there: every step of an orchestration, one step, or a tool or skill
#: of the same name. Marked destructive, so a client asks its person first.
OVERWRITES: frozenset[str] = frozenset({
    "update_orchestration",
    "remove_step",
    "create_http_tool",
    "create_skill",
})

#: Acts that reach outside the workspace.
OPEN_WORLD: frozenset[str] = frozenset({"import_openapi"})

_TARGET_ARGS: tuple[tuple[str, str], ...] = (
    ("orch_id", "orchestration"), ("agent_id", "agent"), ("schedule_id", "schedule"),
)

_MODULE: contextvars.ContextVar[Any] = contextvars.ContextVar("product_tools_server_module", default=None)


@contextlib.contextmanager
def using_module(module: Any) -> Iterator[None]:
    """The tool sessions a builder tool is handed (`server_module`), for the calls made inside: the MCP server's
    per-tenant module in cloud, this process's own by default."""
    token = _MODULE.set(module)
    try:
        yield
    finally:
        _MODULE.reset(token)


def _target(action: str):
    kind = action.split(".", 1)[0]

    def target(args: dict[str, Any], result: Any) -> tuple[str, str] | None:
        for key, name in _TARGET_ARGS:
            if args.get(key):
                return name, str(args[key])
        try:
            parsed = json.loads(result) if isinstance(result, str) else result
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            for key in ("id", "orch_id", "agent_id", "schedule_id"):
                if parsed.get(key):
                    return kind, str(parsed[key])
        if args.get("name"):
            return kind, str(args["name"])
        return None

    return target


def _handler(name: str):
    async def handle(caller: Any, args: dict[str, Any]) -> str:
        from core.builder_tools import execute_builder_tool_with_events

        module = _MODULE.get()
        if module is None:
            import core.server as module  # this process's own tool sessions
        # The builder's UI events have no stream to go to from here (F695).
        output, _events = await execute_builder_tool_with_events(name, args, module)
        try:
            parsed = json.loads(output)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict) and "error" in parsed:
            raise ToolRefusal(output)
        return output

    return handle


def _offered(name: str):
    def offered() -> bool:
        from core.builder_tools import builder_tool_offered
        return builder_tool_offered(name)

    return offered


def entries() -> list[ProductTool]:
    from core.builder_tools import BUILDER_TOOL_SCHEMAS

    by_name = {s["function"]["name"]: s["function"] for s in BUILDER_TOOL_SCHEMAS}
    missing = [n for n in (*READS, *ACTS) if n not in by_name]
    if missing:
        # A renamed builder tool must fail loudly here, not vanish from MCP.
        raise RuntimeError(f"product tools name builder tools that do not exist: {missing}")

    def schema(name: str) -> dict:
        return by_name[name].get("parameters") or {"type": "object", "properties": {}}

    tools = [
        ProductTool(name=n, description=by_name[n].get("description", ""), input_schema=schema(n),
                    handler=_handler(n), kind="read", area="build", offered=_offered(n))
        for n in READS
    ]
    tools += [
        ProductTool(name=n, description=by_name[n].get("description", ""), input_schema=schema(n),
                    handler=_handler(n), kind="act", area="build", min_role=Role.BUILDER, scope="build",
                    destructive=n in OVERWRITES, open_world=n in OPEN_WORLD, audit_action=action,
                    target=_target(action), offered=_offered(n))
        for n, action in ACTS.items()
    ]
    return tools
