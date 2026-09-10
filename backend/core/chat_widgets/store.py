"""
Persistent store for embeddable chat widgets.

One collection, ``chat_widgets``, one document per widget. Modelled on
``core.messaging.store`` — same ``core.store.collections`` substrate, same
create-or-update shape — with one deliberate difference.

**A widget carries `agent_id` as a scalar, and there is no join.** Channels need
the many-to-many (`channel_assignments`) because a Slack workspace can front
several agents and a visitor there can pick one with `/agent`. A widget visitor
is anonymous on somebody else's marketing page; there is no picker to offer and
nothing to pick with. One widget answers as one agent, and a second agent means
a second widget with its own key and its own allow-list — which is also the
honest unit for the origin check, since the two agents may belong on different
sites.

`public_key` is stored in the clear on purpose. It ships in the customer's HTML;
hashing it would protect nothing and cost a lookup index. What authorises a
conversation is `allowed_origins` — see `core.chat_widgets.origins`.
"""
from __future__ import annotations

import time
from typing import Optional

from core.chat_widgets import origins as _origins
from core.widget_token import new_public_key

_COLLECTION = "chat_widgets"

#: A visitor cannot be asked to approve a tool call — they are not a member, have
#: no role, and cannot be trusted with a destructive one. So a widget refuses the
#: whole class at bind time. See `gating_categories`.
GATING_DECISIONS = frozenset(("confirm",))

#: Fallbacks for a widget the customer has not themed. Kept here rather than in
#: the UI so the embed page and the settings screen cannot disagree.
DEFAULTS: dict[str, object] = {
    "enabled": True,
    "greeting": "Hi! How can I help?",
    "accent": "#4f46e5",
    "position": "right",
    "launcher_label": "Chat",
    "title": "",
    "daily_message_cap": 500,
}


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


async def _load_raw() -> list[dict]:
    from core.store import collections
    return await collections.load(_COLLECTION)


async def _save_raw(data: list[dict]) -> None:
    from core.store import collections
    await collections.save(_COLLECTION, data)


def public_view(widget: dict) -> dict:
    """What the browser inside the iframe may see.

    The allow-list is **not** in here. It is configuration the owner reads on the
    settings screen, and telling a visitor which other sites run this widget
    answers a question nobody asked them to ask.
    """
    return {
        "id": widget.get("id"),
        "agent_id": widget.get("agent_id"),
        "greeting": widget.get("greeting") or DEFAULTS["greeting"],
        "accent": widget.get("accent") or DEFAULTS["accent"],
        "position": widget.get("position") or DEFAULTS["position"],
        "launcher_label": widget.get("launcher_label") or DEFAULTS["launcher_label"],
        "title": widget.get("title") or "",
    }


async def list_widgets() -> list[dict]:
    return await _load_raw()


async def get_widget(widget_id: str) -> Optional[dict]:
    data = await _load_raw()
    return next((w for w in data if w.get("id") == widget_id), None)


async def get_by_public_key(public_key: str) -> Optional[dict]:
    """The one lookup the embed page makes, before anyone is authenticated.

    Returns the row whatever its `enabled` flag says — the caller needs to tell
    "no such widget" from "switched off" so it can say which, and both answers
    are already known to whoever holds the key.
    """
    if not public_key:
        return None
    data = await _load_raw()
    return next((w for w in data if w.get("public_key") == public_key), None)


async def save_widget(widget: dict) -> dict:
    """Create or update. Mints the id and the public key on create."""
    data = await _load_raw()
    now = _now()

    if not widget.get("id"):
        widget["id"] = f"wgt_{new_public_key()[3:11]}"
        widget["created_at"] = now
    if not widget.get("public_key"):
        widget["public_key"] = new_public_key()

    widget["updated_at"] = now
    for field, value in DEFAULTS.items():
        widget.setdefault(field, value)
    widget["allowed_origins"] = _origins.clean_list(widget.get("allowed_origins"))

    idx = next((i for i, w in enumerate(data) if w.get("id") == widget["id"]), None)
    if idx is not None:
        existing = data[idx]
        widget["created_at"] = existing.get("created_at", now)
        # The key is the customer's published address. A save that omitted it —
        # renaming the widget, say — must never rotate it and break every page
        # that already carries the snippet.
        widget["public_key"] = existing.get("public_key") or widget["public_key"]
        data[idx] = widget
    else:
        data.append(widget)

    await _save_raw(data)
    return widget


async def delete_widget(widget_id: str) -> bool:
    data = await _load_raw()
    remaining = [w for w in data if w.get("id") != widget_id]
    if len(remaining) == len(data):
        return False
    await _save_raw(remaining)
    return True


async def rotate_public_key(widget_id: str) -> Optional[dict]:
    """A new key, for when the old one is being abused.

    The only revocation this feature has that is faster than the token TTL: the
    old key stops resolving immediately, so no new conversation starts on it.
    Conversations already holding a visitor token run to its expiry.
    """
    data = await _load_raw()
    idx = next((i for i, w in enumerate(data) if w.get("id") == widget_id), None)
    if idx is None:
        return None
    # Written here rather than through `save_widget`, which deliberately refuses
    # to change a key: an ordinary save must never rotate the customer's
    # published address, so rotation cannot go through the path that enforces
    # that. Two callers, two rules, and neither can be mistaken for the other.
    data[idx]["public_key"] = new_public_key()
    data[idx]["updated_at"] = _now()
    await _save_raw(data)
    return data[idx]


def gating_categories(agent: dict | None, settings: dict | None) -> list[str]:
    """The tool-policy categories that would stop and ask a person.

    A widget refuses to bind an agent with any of these, because the approval
    card cannot be shown to an anonymous visitor: they are not a member, have no
    role, and the card renders the tool's raw arguments — `run_sql_query` is one
    door for `SELECT` and `DROP`. Refusing here means the failure lands in front
    of the person who can fix it, rather than in front of a stranger mid-sentence.

    `deny` is not in this set. A denied call never asks anyone anything; it is
    refused and the model is told why, which is exactly the behaviour a widget
    wants.
    """
    from core.tool_policy import CATEGORIES, resolve_policy

    policy = resolve_policy(agent, settings)
    return [c for c in CATEGORIES if policy.get(c) in GATING_DECISIONS]
