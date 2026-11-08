"""The secret values this install holds, found in a piece of text (F1307, TRI1).

CAP3's cap-03: the person pasted their tracker's MCP token bare, on its own turn. It has no shape the scrubber knows,
so it reached the builder's runs, its work cards, its logs and blobs — while the workspace already held that exact
value, saved on the server's Connect card. The door now masks any value the workspace holds as a secret, by exact
match (`core.native_builder.door.check(held=…)`), before anything stores, logs or shows the text.

This is the self-hosted finder (`core.secret_scrub.set_held_finder` lets a host put its own: cloud's keeps fingerprints,
never plaintext). It reads, each time it is asked — a value saved on its card a moment ago is held at once:

* the MCP servers' definitions (a token, its headers and env, anything under a secret-named key) and their OAuth
  tokens (`mcp_tokens`): a connection's;
* the channels' credentials (`messaging.store.SECRET_CREDENTIALS`): a connection's;
* the HTTP tools' header values, and the token after "Bearer ": a connection's;
* the settings and variables under a secret-named key (a provider's key, a token): the workspace's.

A reference kept in any of them (`{{ global.X }}`, `${X}`) names a value kept elsewhere and is never held (F1326).

It returns only the held values that occur in the text, so no value leaves here that was not in the person's own words,
and nothing here is logged.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

#: A key whose value is a secret: a token, a password, a key, a header that authorises.
_SECRET_KEY = re.compile(r"(?i)(token|secret|password|passwd|api[_-]?key|apikey|authorization|credential|"
                         r"(?:^|[_-])key$)")
#: The kind of holder, for the person's notice: a connection's card, or the workspace's own.
CONNECTION, WORKSPACE = "connection", "workspace"
#: A reference names a value kept elsewhere (`{{ global.X }}`, `${X}`): a name, never a held secret (F1326), as
#: F1309's header check reads one. A person who pastes the same reference keeps it.
_REFERENCE = re.compile(r"\{\{|\$\{[A-Za-z_]")


def _values_under(value: Any, *, all_strings: bool = False, key: str = "") -> Iterable[str]:
    """Every string under a secret-named key in `value` (or every string, for a holder whose values are all secret);
    never a reference."""
    if isinstance(value, str):
        if (all_strings or _SECRET_KEY.search(key)) and not _REFERENCE.search(value):
            yield value
            if value.lower().startswith("bearer "):
                yield value[7:].strip()
    elif isinstance(value, dict):
        for k, v in value.items():
            inner = all_strings or k in ("headers", "env") or bool(_SECRET_KEY.search(str(k)))
            yield from _values_under(v, all_strings=inner, key=str(k))
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _values_under(v, all_strings=all_strings, key=key)


async def _held(*, with_variables: bool = True) -> list[tuple[str, str]]:
    """(value, kind) for every secret this install holds. A source that can't be read is skipped on its own, logged by
    its error's type only (its words could carry a value), so the others still count. `with_variables` False leaves
    the workspace's variables out (the output side's matcher: see `matcher`)."""
    import logging

    from core import variables
    from core.messaging import store as channels
    from core.store import collections
    from core.store.resources import load_mcp_servers, load_tools
    from core.store.settings import load_settings_for_tenant

    async def servers():
        return [(v, CONNECTION) for server in await load_mcp_servers() for v in _values_under(server)]

    async def oauth():
        return [(v, CONNECTION) for item in await collections.load("mcp_tokens") for v in _values_under(item)]

    async def channel_tokens():
        return [(str(v), CONNECTION) for channel in await channels.list_channels()
                for k, v in (channel.get("credentials") or {}).items() if k in channels.SECRET_CREDENTIALS and v]

    async def tool_headers():
        return [(v, CONNECTION) for tool in await load_tools()
                for v in _values_under(tool.get("headers") or {}, all_strings=True)]

    async def settings():
        return [(v, WORKSPACE) for v in _values_under(await load_settings_for_tenant())]

    async def workspace_variables():
        return [(v, WORKSPACE) for v in _values_under(await variables.load())]

    held: list[tuple[str, str]] = []
    sources = (servers, oauth, channel_tokens, tool_headers, settings) + ((workspace_variables,) if with_variables else ())
    for source in sources:
        try:
            held += await source()
        except Exception as exc:  # noqa: BLE001 — one store unread leaves the others' values held
            logging.getLogger(__name__).warning("held secrets: %s could not be read (%s)", source.__name__,
                                                type(exc).__name__)
    return held


async def find(text: str) -> list[tuple[str, str]]:
    """The held secrets in `text`, as (as written, kind): each once, the longest first. A value written with
    lookalikes (a no-break hyphen for its "-", F1339) comes back as it stands in `text`, so the door masks exactly that."""
    from core.secret_scrub import secret_like, written_as

    kinds: dict[str, str] = {}
    for value, kind in await _held():
        if secret_like(value):
            kinds.setdefault(value, kind)
    found: dict[str, str] = {}
    for value, written in written_as(text, list(kinds)):
        found.setdefault(written, kinds[value])
    return sorted(found.items(), key=lambda vk: -len(vk[0]))


class _Values:
    """This step's held values, longest first: `find` names those in a text as they stand in it, lookalikes and all
    (`secret_scrub.refresh_held`, F1339)."""

    def __init__(self, values: Iterable[str]):
        self._values = sorted({v for v in values}, key=len, reverse=True)

    def find(self, text: str) -> list[str]:
        from core.secret_scrub import written_as

        return [written for _, written in written_as(text, self._values)]


async def matcher() -> _Values | None:
    """The output side's matcher for this step: every held value, or None when there are none. Not the variables: a
    workflow renders `{{ global.X }}` into its steps on purpose, and its run shows the value as it always has; one
    stored as a secret is held already (`variables.load`, F854) and leaves as its name. So a step's start reads no
    variables store either (the run loads them once)."""
    from core.secret_scrub import secret_like

    values = [v for v, _ in await _held(with_variables=False) if secret_like(v)]
    return _Values(values) if values else None
