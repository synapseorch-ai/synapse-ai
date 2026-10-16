"""Knowledge's search, as a product tool (MCP2, D292): a read, listed while Knowledge is on (D176).

Its code is `core.mcp_server.knowledge`, with the `set_knowledge_searcher` seam cloud installs; the caller's
credential is handed on, as before, so a search's trace names the key that asked.
"""
from __future__ import annotations

from typing import Any

from core.product_tools.registry import ProductTool


async def _search(caller: Any, args: dict[str, Any]) -> str:
    from core.mcp_server import knowledge
    return await knowledge.search_tool(args, caller.principal)


def _on() -> bool:
    from core.knowledge.config import knowledge_enabled
    return knowledge_enabled()


def entries() -> list[ProductTool]:
    from core.mcp_server import knowledge

    return [ProductTool(name=knowledge.NAME, description=knowledge.DESCRIPTION,
                        input_schema=dict(knowledge.SEARCH_SCHEMA), handler=_search, kind="read",
                        area="knowledge", offered=_on)]
