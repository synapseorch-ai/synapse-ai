"""An agent's memory items, read, corrected and deleted as the agent's Memory screen does (F1259).

The same doors as the screen (`core/routes/agent_memory.py`): `agent_memory.write_memory` corrects one (re-indexed,
as the newest) and `delete_memory` deletes one with its index line. Never looser than the screen: what is written is
held to `secret_scrub.holds_credential` too, so a key never becomes a standing memory. An item is named
`<agent id>/<item name>`, as `list_agent_memory` lists them.
"""
from __future__ import annotations

from typing import Any

from core.product_tools.errors import ToolRefusal
from core.product_tools.registry import ProductTool
from core.product_tools.roles import Role

_ITEM = {"item": {"type": "string", "description": "The memory item: `<agent id>/<item name>`."}}


async def _agent(agent_id: str) -> dict:
    from core.store.resources import get_agent

    agent = await get_agent(str(agent_id or ""))
    if agent is None or agent.get("system"):
        raise ToolRefusal("There is no such agent.")
    return agent


def split(item: str) -> tuple[str, str]:
    """`<agent id>/<item name>` as its two parts."""
    from core.agent_memory import normalize_name

    agent_id, _, name = str(item or "").partition("/")
    if not agent_id or not name:
        raise ToolRefusal("Name the memory item as `<agent id>/<item name>` (list_agent_memory lists them).")
    return agent_id, normalize_name(name)


async def items(agent: dict) -> list[dict]:
    """An agent's memory items, newest first: each by its id, with its first line."""
    from core.agent_memory import memory_view

    return [{"id": f"{agent['id']}/{i['name']}", "name": i["name"], "summary": i["summary"],
             "archived": i["archived"]} for i in memory_view(agent["id"])["items"]]


async def _list(caller: Any, args: dict[str, Any]) -> dict:
    agent = await _agent(str(args.get("agent_id") or ""))
    return {"agent": agent.get("name"), "memory": bool(agent.get("memory")), "items": await items(agent)}


async def _edit(caller: Any, args: dict[str, Any]) -> dict:
    from core import agent_memory
    from core.secret_scrub import holds_credential

    agent_id, name = split(str(args.get("item") or ""))
    await _agent(agent_id)
    content = str(args.get("content") or "")
    if holds_credential(content):
        raise ToolRefusal("A memory never keeps a key: an agent would read it in every prompt. Keep the key in the "
                          "Vault, and if it was real, rotate it at the provider.")
    saved, error = await agent_memory.write_memory(agent_id, name, content)
    if error:
        raise ToolRefusal(f"Not saved: {error}.")
    return {"id": f"{agent_id}/{saved}", "item": f"{agent_id}/{saved}"}


async def _delete(caller: Any, args: dict[str, Any]) -> dict:
    from core import agent_memory

    agent_id, name = split(str(args.get("item") or ""))
    await _agent(agent_id)
    if not await agent_memory.delete_memory(agent_id, name):
        raise ToolRefusal("That is not in this agent's memory.")
    return {"deleted": f"{agent_id}/{name}"}


async def _preview_delete(caller: Any, args: dict[str, Any]) -> dict:
    """What deleting the item removes, before the confirmation (MCP2's token)."""
    from core.agent_memory import read_file

    agent_id, name = split(str(args.get("item") or ""))
    agent = await _agent(agent_id)
    text = read_file(agent_id, "items", name)
    if text is None:
        raise ToolRefusal("That is not in this agent's memory.")
    return {"deletes": f'the memory item "{name}" of the agent "{agent.get("name")}"',
            "with_it": "its index line, so the agent no longer reads it"}


def entries() -> list[ProductTool]:
    act = {"kind": "act", "area": "agents", "min_role": Role.BUILDER, "scope": "build"}
    return [
        ProductTool(name="list_agent_memory", kind="read", area="agents",
                    description="An agent's memory items, newest first: each by its id, with its first line.",
                    input_schema={"type": "object", "properties": {"agent_id": {"type": "string"}},
                                  "required": ["agent_id"]}, handler=_list),
        ProductTool(name="edit_agent_memory_item",
                    description="Correct one of an agent's memory items (or write one back): it is indexed again as "
                                "the newest. A key is never kept.",
                    input_schema={"type": "object", "properties": {**_ITEM, "content": {"type": "string"}},
                                  "required": ["item", "content"]},
                    handler=_edit, audit_action="agent.memory.edited",
                    target=lambda a, r: ("agent", split(str(a.get("item") or ""))[0]), **act),
        ProductTool(name="delete_agent_memory_item",
                    description="Delete one of an agent's memory items, with its index line.",
                    input_schema={"type": "object", "properties": dict(_ITEM), "required": ["item"]},
                    handler=_delete, irreversible=True, preview=_preview_delete,
                    audit_action="agent.memory.deleted",
                    target=lambda a, r: ("agent", split(str(a.get("item") or ""))[0]), **act),
    ]
