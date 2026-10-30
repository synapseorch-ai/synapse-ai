"""Which modules hold product tools, and loading them into the registry (MCP2, D292).

Each area module has `entries() -> list[ProductTool]`. An adapter calls `ensure_loaded()` before it lists or calls
anything; a new area is a module here and a line in `AREAS`.
"""
from __future__ import annotations

import importlib

from core.product_tools import registry

AREAS: tuple[str, ...] = (
    "core.product_tools.build",
    "core.product_tools.runs",
    "core.product_tools.knowledge",
    "core.product_tools.approvals",
    "core.product_tools.activity",
    "core.product_tools.schedules",
    "core.product_tools.boards",
    "core.product_tools.usage",
    "core.product_tools.browser",
    "core.product_tools.features",
    "core.product_tools.evals",
    "core.product_tools.settings",
)

_loaded_into: int | None = None


def load() -> None:
    """Register every area's entries, replacing any of the same name."""
    global _loaded_into
    for path in AREAS:
        for tool in importlib.import_module(path).entries():
            registry.register(tool, replace=True)
    _loaded_into = id(registry._TOOLS)


def ensure_loaded() -> None:
    if _loaded_into != id(registry._TOOLS):
        load()
