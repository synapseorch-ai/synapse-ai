"""
Channels standalone, agents assigned — the many-to-many join.

The join is authoritative and the legacy `agent_id` scalar is a read-time
fallback for documents that predate it. What these tests hold in place:
re-binding through the join genuinely re-binds (a stale scalar must not keep
answering), the single-agent form's scalar keeps working through the mirror,
and a channel's assignments die with it.
"""
import pytest

from core.messaging import store


def _channel(cid=None, **overrides):
    ch = {
        "name": "Test channel",
        "platform": "telegram",
        "credentials": {"bot_token": "123:abc"},
        "enabled": True,
    }
    if cid:
        ch["id"] = cid
    ch.update(overrides)
    return ch


async def test_channel_created_standalone_gets_defaults():
    saved = await store.save_channel(_channel())
    assert saved["id"].startswith("chn_")
    assert saved["mode"] == "polling"
    assert saved["default_conversation_id"] is None
    assert await store.get_agent_ids_for_channel(saved["id"]) == []


async def test_assign_is_many_to_many_and_idempotent():
    a = await store.save_channel(_channel())
    b = await store.save_channel(_channel())

    await store.assign_channel(a["id"], "agent_1")
    await store.assign_channel(a["id"], "agent_2")
    await store.assign_channel(b["id"], "agent_1")
    await store.assign_channel(a["id"], "agent_1")  # again — no duplicate

    assert await store.get_agent_ids_for_channel(a["id"]) == ["agent_1", "agent_2"]
    mine = await store.get_channels_for_agent("agent_1")
    assert {c["id"] for c in mine} == {a["id"], b["id"]}
    assert len(await store.list_assignments(a["id"])) == 2


async def test_first_assignment_is_default_and_the_flag_moves():
    ch = await store.save_channel(_channel())
    await store.assign_channel(ch["id"], "agent_1")
    await store.assign_channel(ch["id"], "agent_2")
    assert await store.get_default_agent_id(ch["id"]) == "agent_1"

    await store.assign_channel(ch["id"], "agent_2", is_default=True)
    assert await store.get_default_agent_id(ch["id"]) == "agent_2"
    # Exactly one default
    defaults = [a for a in await store.list_assignments(ch["id"]) if a["is_default"]]
    assert len(defaults) == 1


async def test_unassign_hands_the_default_to_a_survivor():
    ch = await store.save_channel(_channel())
    await store.assign_channel(ch["id"], "agent_1")
    await store.assign_channel(ch["id"], "agent_2")

    assert await store.unassign_channel(ch["id"], "agent_1") is True
    assert await store.get_default_agent_id(ch["id"]) == "agent_2"
    assert await store.unassign_channel(ch["id"], "agent_1") is False


async def test_legacy_scalar_answers_only_until_the_join_speaks():
    # A pre-join document: agent_id in the doc, no assignment rows. Written
    # through the raw store to model data that predates save_channel's mirror.
    from core.store import collections

    await collections.save(
        "messaging_channels",
        [{"id": "chn_old", "platform": "telegram", "agent_id": "agent_legacy"}],
    )

    mine = await store.get_channels_for_agent("agent_legacy")
    assert [c["id"] for c in mine] == ["chn_old"]
    assert await store.get_agent_ids_for_channel("chn_old") == ["agent_legacy"]

    # The moment the join has a row for this channel, the scalar stops
    # answering — re-binding genuinely re-binds.
    await store.assign_channel("chn_old", "agent_new")
    assert await store.get_channels_for_agent("agent_legacy") == []
    assert await store.get_agent_ids_for_channel("chn_old") == ["agent_new"]


async def test_save_channel_mirrors_the_scalar_into_the_join():
    ch = await store.save_channel(_channel(agent_id="agent_1"))
    assert await store.get_agent_ids_for_channel(ch["id"]) == ["agent_1"]

    # A picker-made assignment for another agent survives the scalar changing;
    # the scalar's own old assignment is replaced.
    await store.assign_channel(ch["id"], "agent_3")
    ch["agent_id"] = "agent_2"
    await store.save_channel(ch)
    assert set(await store.get_agent_ids_for_channel(ch["id"])) == {"agent_2", "agent_3"}


async def test_delete_channel_takes_its_assignments_with_it():
    ch = await store.save_channel(_channel())
    await store.assign_channel(ch["id"], "agent_1")

    assert await store.delete_channel(ch["id"]) is True
    assert await store.list_assignments(ch["id"]) == []
    assert await store.get_channels_for_agent("agent_1") == []


async def test_default_conversation_is_learned_and_kept():
    ch = await store.save_channel(_channel())
    await store.set_default_conversation(ch["id"], "12345")
    assert (await store.get_channel(ch["id"]))["default_conversation_id"] == "12345"
    # Idempotent — a repeat of the same value is not a write worth making.
    await store.set_default_conversation(ch["id"], "12345")
    assert (await store.get_channel(ch["id"]))["default_conversation_id"] == "12345"
