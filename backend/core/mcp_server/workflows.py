"""Every workspace orchestration as its own MCP tool (13's A7, D125).

`run_orchestration` can start any of them by id, but a client model picks tools
by name and description: a workflow called "Weekly client summary" is found as
`run_weekly_client_summary`, described in its own words, far more often than it
is found by listing orchestrations first.

The list is recomputed on every `tools/list`. Stateless HTTP cannot push
`tools/list_changed`, so a client sees a new workflow the next time it lists.
"""
from __future__ import annotations

import re

from mcp import types

from core.mcp_server.runs import DEFAULT_WAIT_SECONDS, _wait_prop

PREFIX = "run_"
_MAX_NAME = 64
_JSON_TYPES = {"string", "number", "integer", "boolean", "array", "object"}
# Argument names the tool itself uses; a state key cannot shadow them.
_OWN_ARGS = {"message", "wait_seconds"}


def _slug(text: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", (text or "").lower())).strip("_")


def _declared_inputs(orch: dict) -> dict[str, dict]:
    """State keys a caller may set: described in the schema, written by no step."""
    written = {s.get("output_key") for s in orch.get("steps") or [] if s.get("output_key")}
    inputs: dict[str, dict] = {}
    for key, spec in (orch.get("state_schema") or {}).items():
        if not isinstance(spec, dict) or not spec.get("description"):
            continue
        if key in written or key in _OWN_ARGS or key.startswith("_"):
            continue
        prop: dict = {"description": str(spec["description"])}
        if spec.get("type") in _JSON_TYPES:
            prop["type"] = spec["type"]
        inputs[key] = prop
    return inputs


def name_workflows(orchs: list[dict], reserved: set[str]) -> dict[str, dict]:
    """Tool name → orchestration, stable for a given list.

    A name is `run_` plus the orchestration's name as a slug, cut to MCP's 64
    characters. Two orchestrations with one slug, or a slug that lands on a
    fixed tool's name, keep the first and suffix the rest with the id's tail.
    """
    taken = set(reserved)
    named: dict[str, dict] = {}
    for orch in sorted(orchs, key=lambda o: (o.get("created_at") or "", o.get("id") or "")):
        oid = str(orch.get("id") or "")
        base = PREFIX + (_slug(orch.get("name", "")) or _slug(oid))
        name = base[:_MAX_NAME]
        if name in taken:
            tail = "_" + (_slug(oid)[-8:] or "x")
            name = base[: _MAX_NAME - len(tail)] + tail
            n = 2
            while name in taken:
                suffix = f"{tail}{n}"
                name = base[: _MAX_NAME - len(suffix)] + suffix
                n += 1
        taken.add(name)
        named[name] = orch
    return named


async def workflow_map(reserved: set[str]) -> dict[str, dict]:
    from core.builder_tools import _is_visible
    from core.routes.orchestrations import load_orchestrations

    return name_workflows([o for o in await load_orchestrations() if _is_visible(o) and o.get("id")], reserved)


def as_tool(name: str, orch: dict) -> types.Tool:
    label = orch.get("name") or orch.get("id")
    about = (orch.get("description") or "").strip() or f"Runs the '{label}' workflow."
    inputs = _declared_inputs(orch)
    return types.Tool(
        name=name,
        title=str(label),
        description=(
            f"{about}\n\nA Synapse workflow ('{label}', id {orch.get('id')}). Waits for it to finish "
            "or ask a question: `paused` carries the question — answer it with answer_run — and "
            "`running` means the wait ran out (follow it with get_run). Runs spend model credits."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "What this run should work on."},
                **inputs,
                "wait_seconds": _wait_prop(DEFAULT_WAIT_SECONDS),
            },
            "required": ["message"],
        },
        annotations=types.ToolAnnotations(
            title=str(label), readOnlyHint=False, destructiveHint=False, openWorldHint=True,
        ),
    )


def split_args(orch: dict, args: dict) -> tuple[str, dict | None]:
    """A workflow tool's arguments as (message, initial_state)."""
    declared = _declared_inputs(orch)
    inputs = {k: v for k, v in args.items() if k in declared}
    return str(args.get("message") or ""), (inputs or None)
