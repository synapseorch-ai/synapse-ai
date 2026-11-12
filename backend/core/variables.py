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

from contextvars import ContextVar
from typing import Any

COLLECTION = "global_variables"

#: Keys must be usable inside `{{ global.<key> }}`.
KEY_MAX = 64


def valid_key(key: str) -> bool:
    return bool(key) and len(key) <= KEY_MAX and key.replace("_", "a").replace("-", "a").isalnum() and not key[0].isdigit()


async def load() -> dict[str, Any]:
    """The tenant's variables, `{key: value}`. Empty when none are set.

    A value the store's resolver swapped — a secret stored as a reference — is
    held for this task (`core.secret_scrub.hold_secrets`), so whatever the task
    then stores or shows names it rather than carrying its plaintext (F854).
    """
    from core.secret_scrub import hold_secrets
    from core.store import collections

    stored = await collections.load_one(COLLECTION, default={}, resolve=False)
    document = await collections._resolved(stored)
    values = _values(document)
    raw = _values(stored)
    secrets = {k: v for k, v in values.items() if k in raw and raw[k] != v}
    hold_secrets(secrets)
    _SECRET_NAMES.set(frozenset(secrets))
    return values


#: The variables the last `load` in this task found stored as secret references (F1556): every one, however short its
#: value (`hold_secrets` holds only the long), so a person's text never shows one.
_SECRET_NAMES: ContextVar[frozenset[str]] = ContextVar("secret_variable_names", default=frozenset())


def secret_names() -> frozenset[str]:
    """The names of the variables the last `load` in this task found to be secrets."""
    return _SECRET_NAMES.get()


def named(values: dict[str, Any]) -> list[dict[str, str]]:
    """Each variable by name, with the reference that uses it, never its value (F1271): what the builder and
    Synapse AI are told of them."""
    return [{"key": key, "reference": f"{{{{ global.{key} }}}}"} for key in sorted(values)]


def _values(document: Any) -> dict[str, Any]:
    values = document.get("values") if isinstance(document, dict) else None
    return dict(values) if isinstance(values, dict) else {}


async def save(values: dict[str, Any]) -> None:
    from core.store import collections

    await collections.save_one(COLLECTION, {"values": dict(values)})
