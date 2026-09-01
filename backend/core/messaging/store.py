"""
Persistent store for messaging channels and their agent assignments.

Two collections:

* ``messaging_channels`` — one document per channel: platform, credentials,
  enabled flag, runtime status. Created *standalone*; a channel is a connection
  to a workspace, not a property of an agent.
* ``channel_assignments`` — the many-to-many join between channels and agents.
  One row per (channel, agent): a channel can front several agents and an agent
  can answer on several channels, which is why this is a join and not a column
  on either side.

A human step's channel is deliberately **not** in the join. That binding lives
in the step document (``StepConfig.human_channel_id``), because it belongs to
the orchestration definition: it exports, versions and deletes with the
workflow, and nothing ever asks "which steps use this channel" — an inbound
reply is matched against the *pending question*, which is runtime state, not
configuration.

Compatibility: channels saved before the join carried a single ``agent_id``
scalar. ``save_channel`` mirrors that field into an assignment so old callers
(and the settings UI's single-agent form) keep working, and
``get_channels_for_agent`` still honours the scalar on documents that predate
the join and were never re-saved.
"""
import time
import uuid
from typing import Optional


_COLLECTION = "messaging_channels"
_ASSIGNMENTS = "channel_assignments"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


async def _load_raw() -> list[dict]:
    from core.store import collections
    return await collections.load(_COLLECTION)


async def _save_raw(data: list[dict]) -> None:
    from core.store import collections
    await collections.save(_COLLECTION, data)


async def _load_assignments() -> list[dict]:
    from core.store import collections
    return await collections.load(_ASSIGNMENTS)


async def _save_assignments(data: list[dict]) -> None:
    from core.store import collections
    await collections.save(_ASSIGNMENTS, data)


# --------------------------------------------------------------------------- #
# Channels
# --------------------------------------------------------------------------- #

async def list_channels() -> list[dict]:
    """Return all channel configs."""
    return await _load_raw()


async def get_channel(channel_id: str) -> Optional[dict]:
    """Return one channel by id, or None."""
    return next((c for c in await _load_raw() if c["id"] == channel_id), None)


async def get_channels_for_agent(agent_id: str) -> list[dict]:
    """Every channel assigned to this agent.

    The join is authoritative. The legacy ``agent_id`` scalar is honoured only
    for channels that have **no** assignment rows at all — a document written
    before the join existed and never re-saved. A channel with assignments and
    a stale scalar answers from its assignments, so re-binding through the join
    genuinely re-binds.
    """
    assignments = await _load_assignments()
    assigned_channels = {a["channel_id"] for a in assignments}
    mine = {
        a["channel_id"]
        for a in assignments
        if a.get("target_type", "agent") == "agent" and a.get("target_id") == agent_id
    }
    return [
        c
        for c in await _load_raw()
        if c["id"] in mine
        or (c["id"] not in assigned_channels and c.get("agent_id") == agent_id)
    ]


async def save_channel(channel: dict) -> dict:
    """Create or update a channel. Auto-assigns id and timestamps.

    A caller that still hands in ``agent_id`` gets the join maintained for it:
    the assignment for the new agent is ensured, and — because that caller's
    mental model is "the channel's one agent" — a *changed* scalar replaces the
    old agent's assignment rather than accumulating beside it. Assignments made
    through :func:`assign_channel` for other agents are left alone.
    """
    data = await _load_raw()
    now = _now()

    if not channel.get("id"):
        channel["id"] = f"chn_{uuid.uuid4().hex[:8]}"
        channel["created_at"] = now

    channel["updated_at"] = now
    # Set defaults
    channel.setdefault("status", "stopped")
    channel.setdefault("last_error", None)
    channel.setdefault("enabled", True)
    channel.setdefault("multi_agent_mode", False)
    # "polling" is what every pre-existing channel was doing; "webhook" is the
    # opt-in for deployments that have a public URL (see the adapters).
    channel.setdefault("mode", "polling")
    # Where proactive messages (schedule results, human-step prompts) go when
    # nothing more specific is known. Learned from traffic for platforms where
    # it cannot be configured up front (a Telegram chat id only exists once
    # somebody has messaged the bot).
    channel.setdefault("default_conversation_id", None)

    idx = next((i for i, c in enumerate(data) if c["id"] == channel["id"]), None)
    previous_agent_id = None
    if idx is not None:
        # Preserve runtime-only fields from existing record
        existing = data[idx]
        channel["created_at"] = existing.get("created_at", now)
        previous_agent_id = existing.get("agent_id")
        data[idx] = channel
    else:
        data.append(channel)

    await _save_raw(data)

    # Mirror the legacy scalar into the join (see docstring).
    agent_id = channel.get("agent_id")
    if agent_id:
        if previous_agent_id and previous_agent_id != agent_id:
            await unassign_channel(channel["id"], previous_agent_id)
        await assign_channel(channel["id"], agent_id)

    return channel


async def update_channel_status(channel_id: str, status: str, last_error: Optional[str] = None) -> None:
    """Update only the runtime status fields (called by manager)."""
    data = await _load_raw()
    for c in data:
        if c["id"] == channel_id:
            c["status"] = status
            c["last_error"] = last_error
            break
    await _save_raw(data)


async def set_default_conversation(channel_id: str, conversation_id: str) -> None:
    """Record where proactive sends should go, learned from inbound traffic."""
    data = await _load_raw()
    for c in data:
        if c["id"] == channel_id:
            if c.get("default_conversation_id") == conversation_id:
                return
            c["default_conversation_id"] = conversation_id
            break
    await _save_raw(data)


async def delete_channel(channel_id: str) -> bool:
    """Delete a channel and its assignments. Returns True if found and removed."""
    data = await _load_raw()
    new_data = [c for c in data if c["id"] != channel_id]
    if len(new_data) == len(data):
        return False
    await _save_raw(new_data)

    assignments = await _load_assignments()
    remaining = [a for a in assignments if a["channel_id"] != channel_id]
    if len(remaining) != len(assignments):
        await _save_assignments(remaining)
    return True


# --------------------------------------------------------------------------- #
# Assignments — the channel ↔ agent join
# --------------------------------------------------------------------------- #

async def list_assignments(channel_id: Optional[str] = None) -> list[dict]:
    """All assignment rows, optionally for one channel."""
    assignments = await _load_assignments()
    if channel_id is not None:
        assignments = [a for a in assignments if a["channel_id"] == channel_id]
    return assignments


async def assign_channel(channel_id: str, agent_id: str, *, is_default: Optional[bool] = None) -> dict:
    """Bind an agent to a channel. Idempotent on the pair.

    The first agent assigned to a channel becomes its default — the one a plain
    message goes to when several agents share the front door. Passing
    ``is_default=True`` moves the flag; there is exactly one default per
    channel because "who answers" must have exactly one answer.
    """
    assignments = await _load_assignments()
    ours = [a for a in assignments if a["channel_id"] == channel_id]
    existing = next(
        (a for a in ours if a.get("target_type", "agent") == "agent" and a["target_id"] == agent_id),
        None,
    )

    make_default = is_default if is_default is not None else not ours
    if existing is None:
        existing = {
            "id": f"asg_{uuid.uuid4().hex[:8]}",
            "channel_id": channel_id,
            "target_type": "agent",
            "target_id": agent_id,
            "is_default": bool(make_default),
            "created_at": _now(),
        }
        assignments.append(existing)
    elif is_default is not None:
        existing["is_default"] = bool(is_default)

    if existing.get("is_default"):
        for a in assignments:
            if (
                a["channel_id"] == channel_id
                and a["id"] != existing["id"]
                and a.get("is_default")
            ):
                a["is_default"] = False

    await _save_assignments(assignments)
    return existing


async def unassign_channel(channel_id: str, agent_id: str) -> bool:
    """Remove one (channel, agent) binding. Returns True if it existed.

    If the removed row held the default flag, the oldest remaining assignment
    inherits it — a channel with agents but no default would answer nobody.
    """
    assignments = await _load_assignments()
    keep: list[dict] = []
    removed = None
    for a in assignments:
        if (
            removed is None
            and a["channel_id"] == channel_id
            and a.get("target_type", "agent") == "agent"
            and a["target_id"] == agent_id
        ):
            removed = a
        else:
            keep.append(a)
    if removed is None:
        return False

    if removed.get("is_default"):
        heir = next((a for a in keep if a["channel_id"] == channel_id), None)
        if heir is not None:
            heir["is_default"] = True

    await _save_assignments(keep)
    return True


async def get_agent_ids_for_channel(channel_id: str) -> list[str]:
    """Agents assigned to this channel, the default first.

    Falls back to the legacy scalar for pre-join documents, mirroring
    :func:`get_channels_for_agent`.
    """
    ours = [
        a
        for a in await _load_assignments()
        if a["channel_id"] == channel_id and a.get("target_type", "agent") == "agent"
    ]
    if not ours:
        channel = await get_channel(channel_id)
        legacy = (channel or {}).get("agent_id")
        return [legacy] if legacy else []
    ours.sort(key=lambda a: (not a.get("is_default"), a.get("created_at") or ""))
    return [a["target_id"] for a in ours]


async def get_default_agent_id(channel_id: str) -> Optional[str]:
    """The agent a plain message on this channel goes to, or None."""
    agent_ids = await get_agent_ids_for_channel(channel_id)
    return agent_ids[0] if agent_ids else None
