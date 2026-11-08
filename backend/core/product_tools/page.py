"""Every list a product tool returns is paged and size-capped (MCP2, D292).

A client's model reads the whole answer on every turn, so a list never comes back unbounded: at most `limit`
items (`MAX_LIMIT` at most), cut further to stay under a byte budget, with an opaque cursor that continues exactly
where it was cut.
"""
from __future__ import annotations

import base64
import json
from typing import Any

from core.product_tools.errors import ToolRefusal

DEFAULT_LIMIT = 25
MAX_LIMIT = 100
BYTE_CAP = 60_000

PROPERTIES: dict[str, dict] = {
    "cursor": {"type": "string", "description": "From a previous call's next_cursor, for the next page."},
    "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT,
              "description": f"How many at most (default {DEFAULT_LIMIT})."},
}


def _encode(offset: int) -> str:
    return base64.urlsafe_b64encode(f"o:{offset}".encode()).decode().rstrip("=")


def _decode(cursor: Any) -> int:
    if cursor in (None, ""):
        return 0
    try:
        text = base64.urlsafe_b64decode(str(cursor) + "=" * (-len(str(cursor)) % 4)).decode()
        kind, _, number = text.partition(":")
        offset = int(number)
        if kind != "o" or offset < 0:
            raise ValueError
        return offset
    except (ValueError, UnicodeDecodeError):
        raise ToolRefusal("That cursor is not one this tool gave. Leave it out to start from the first page.") from None


def _limit(value: Any, default: int, ceiling: int) -> int:
    try:
        n = int(value) if value is not None else default
    except (TypeError, ValueError):
        n = default
    return max(1, min(n, ceiling))


def page(items: list[Any], args: dict[str, Any], *, default_limit: int = DEFAULT_LIMIT,
         max_limit: int = MAX_LIMIT, byte_cap: int = BYTE_CAP) -> dict[str, Any]:
    offset = _decode(args.get("cursor"))
    chunk = items[offset: offset + _limit(args.get("limit"), default_limit, max_limit)]
    # Cut from the end until it fits; one item always goes, however big, so a page always moves on.
    while len(chunk) > 1 and len(json.dumps(chunk, default=str)) > byte_cap:
        chunk = chunk[:-1]
    end = offset + len(chunk)
    return {"items": chunk, "total": len(items), "next_cursor": _encode(end) if end < len(items) else None}
