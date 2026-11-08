"""Where the builder's tools that live in their own modules register (F1327).

Each such module registered through `core.builder_tools`, which it imported at its top; and `builder_tools` imports
them all again at its end, so that importing it registers every tool. A process whose first import was one of them
therefore ran `builder_tools`' end while that module was still half-loaded, and a module there that takes names from it
at import failed (`change_apply` and `build_plan` from `change_tools`, `build_save` from `build_plan`).

This module imports none of them. A registration made before `builder_tools` is loaded waits here, and is applied, in
the order made, when `builder_tools` attaches its own `register_builder_tool`; one made after is applied at once. So any
module may be imported first, and, imported first or not, `import core.builder_tools` still registers every tool.
"""
from __future__ import annotations

from typing import Any, Callable

_waiting: list[tuple[dict, Any]] = []
_apply: Callable[[dict, Any], None] | None = None


def register_builder_tool(schema: dict, handler: Any) -> None:
    """Register a builder tool (`builder_tools.register_builder_tool`), now or once `builder_tools` is loaded."""
    if _apply is None:
        _waiting.append((schema, handler))
    else:
        _apply(schema, handler)


def attach(apply: Callable[[dict, Any], None]) -> None:
    """`builder_tools` hands over its own registration, once its tables exist: what waited is applied, in order."""
    global _apply
    _apply = apply
    while _waiting:
        apply(*_waiting.pop(0))
