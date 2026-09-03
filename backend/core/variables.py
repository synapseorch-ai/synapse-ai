"""Global variables: one document per tenant, read wherever a prompt or a
tool is rendered.

`{{ global.key }}` (see `core/templating.py`) resolves against this document —
an organisation's base URLs, account ids, tone of voice, the API key it does
not want pasted into fifty tools. Stored as the `global_variables` singleton in
`collections`, so it is tenant-scoped like everything else there, and so a
value that is a secret *reference* (cloud's `synsec://`) is swapped for its
plaintext by the store's document resolver on the way out — the engine never
learns the difference.

Loaded once per run (the orchestration engine keeps it on itself) or once per
chat call; the document is small and the read is one row.
"""
from __future__ import annotations

from typing import Any

COLLECTION = "global_variables"

#: Keys must be usable inside `{{ global.<key> }}`.
KEY_MAX = 64


def valid_key(key: str) -> bool:
    return bool(key) and len(key) <= KEY_MAX and key.replace("_", "a").replace("-", "a").isalnum() and not key[0].isdigit()


async def load() -> dict[str, Any]:
    """The tenant's variables, `{key: value}`. Empty when none are set."""
    from core.store import collections

    document = await collections.load_one(COLLECTION, default={})
    values = document.get("values") if isinstance(document, dict) else None
    return dict(values) if isinstance(values, dict) else {}


async def save(values: dict[str, Any]) -> None:
    from core.store import collections

    await collections.save_one(COLLECTION, {"values": dict(values)})
