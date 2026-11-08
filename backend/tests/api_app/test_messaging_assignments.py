"""
The channel↔agent join through its HTTP surface.

D62 gave the store a many-to-many join and three granular routes; D72 added the
whole-list `PUT`, because a screen made of toggle chips changes a *set* and not
a delta. What these tests hold: a set survives the round trip, the default is an
explicit mark that moves rather than an accident of ordering, the routes refuse
to record an agent that does not exist, and the legacy scalar stops answering
once the join has spoken for a channel.

The manager has to be put on `app.state` by hand: `test_app` does not run the
lifespan, so nothing else would, and every messaging route 503s without it. It
is the real `MessagingManager` with no adapters started — these routes only ever
ask it whether messaging is enabled.
"""
import pytest
import pytest_asyncio

from core.messaging import store as channel_store


@pytest_asyncio.fixture
async def messaging_client(test_app, client):
    """`client`, with messaging enabled on the app."""
    import core.server as server
    from core.messaging.manager import MessagingManager

    test_app.state.messaging_manager = MessagingManager(server_module=server)
    try:
        yield client
    finally:
        test_app.state.messaging_manager = None


async def _seed_agents(*names):
    """Seed several agents at once.

    `seed_agent` writes through `save_user_agents`, which REPLACES the
    collection — so calling it twice leaves one agent, not two.
    """
    from tests._fakes import seed as _seed

    agents = [_seed.make_agent(name=n) for n in names]
    await _seed.seed_agents(agents)
    return agents


async def _make_channel(name="Support"):
    return await channel_store.save_channel(
        {"name": name, "platform": "telegram", "credentials": {"bot_token": "1:x"}}
    )


class TestWholeListAssignments:
    async def test_put_round_trips_a_set(self, messaging_client):
        ch = await _make_channel()
        a, b = await _seed_agents("First", "Second")

        resp = await messaging_client.put(
            f"/api/messaging/channels/{ch['id']}/assignments",
            json={"agent_ids": [a["id"], b["id"]]},
        )
        assert resp.status_code == 200

        listed = await messaging_client.get(
            f"/api/messaging/channels/{ch['id']}/assignments"
        )
        assert {r["target_id"] for r in listed.json()} == {a["id"], b["id"]}
        assert await channel_store.get_default_agent_id(ch["id"]) == a["id"]

    async def test_the_default_is_explicit_and_moves(self, messaging_client):
        ch = await _make_channel()
        a, b = await _seed_agents("First", "Second")
        await messaging_client.put(
            f"/api/messaging/channels/{ch['id']}/assignments",
            json={"agent_ids": [a["id"], b["id"]]},
        )

        resp = await messaging_client.put(
            f"/api/messaging/channels/{ch['id']}/assignments",
            json={"agent_ids": [a["id"], b["id"]], "default_agent_id": b["id"]},
        )
        assert resp.status_code == 200
        assert await channel_store.get_default_agent_id(ch["id"]) == b["id"]
        assert sum(1 for r in resp.json() if r["is_default"]) == 1

        # Re-sending the same membership must not hand the flag back to the
        # first id — an explicit default is a mark, not a position.
        await messaging_client.put(
            f"/api/messaging/channels/{ch['id']}/assignments",
            json={"agent_ids": [a["id"], b["id"]]},
        )
        assert await channel_store.get_default_agent_id(ch["id"]) == b["id"]

    async def test_removing_the_default_promotes_a_survivor(self, messaging_client):
        ch = await _make_channel()
        a, b = await _seed_agents("First", "Second")
        await messaging_client.put(
            f"/api/messaging/channels/{ch['id']}/assignments",
            json={"agent_ids": [a["id"], b["id"]]},
        )
        await messaging_client.put(
            f"/api/messaging/channels/{ch['id']}/assignments",
            json={"agent_ids": [b["id"]]},
        )
        assert await channel_store.get_agent_ids_for_channel(ch["id"]) == [b["id"]]
        assert await channel_store.get_default_agent_id(ch["id"]) == b["id"]

    async def test_an_emptied_channel_has_nobody(self, messaging_client, seed_agent):
        ch = await _make_channel()
        a = await seed_agent()
        await messaging_client.put(
            f"/api/messaging/channels/{ch['id']}/assignments",
            json={"agent_ids": [a["id"]]},
        )
        resp = await messaging_client.put(
            f"/api/messaging/channels/{ch['id']}/assignments", json={"agent_ids": []}
        )
        assert resp.status_code == 200
        assert await channel_store.get_agent_ids_for_channel(ch["id"]) == []
        assert await channel_store.get_default_agent_id(ch["id"]) is None

    async def test_the_channel_list_reflects_a_put(self, messaging_client, seed_agent):
        ch = await _make_channel()
        a = await seed_agent()
        await messaging_client.put(
            f"/api/messaging/channels/{ch['id']}/assignments",
            json={"agent_ids": [a["id"]]},
        )

        listed = (await messaging_client.get("/api/messaging/channels")).json()
        mine = next(c for c in listed if c["id"] == ch["id"])
        assert mine["agent_ids"] == [a["id"]]

        filtered = (
            await messaging_client.get(f"/api/messaging/channels?agent_id={a['id']}")
        ).json()
        assert [c["id"] for c in filtered] == [ch["id"]]


class TestRefusals:
    async def test_an_unknown_agent_is_refused(self, messaging_client):
        ch = await _make_channel()
        (a,) = await _seed_agents("Only")
        resp = await messaging_client.put(
            f"/api/messaging/channels/{ch['id']}/assignments",
            json={"agent_ids": [a["id"], "agent_ghost"]},
        )
        assert resp.status_code == 422
        assert "agent_ghost" in resp.json()["detail"]
        # Nothing was written — a refused set leaves the old one alone.
        assert await channel_store.get_agent_ids_for_channel(ch["id"]) == []

    async def test_an_unknown_channel_is_404(self, messaging_client, seed_agent):
        a = await seed_agent()
        resp = await messaging_client.put(
            "/api/messaging/channels/chn_nope/assignments",
            json={"agent_ids": [a["id"]]},
        )
        assert resp.status_code == 404

    async def test_agent_ids_must_be_a_list(self, messaging_client):
        ch = await _make_channel()
        resp = await messaging_client.put(
            f"/api/messaging/channels/{ch['id']}/assignments",
            json={"agent_ids": "agent_1"},
        )
        assert resp.status_code == 422

    async def test_the_default_must_be_in_the_set(self, messaging_client):
        ch = await _make_channel()
        a, b = await _seed_agents("First", "Second")
        resp = await messaging_client.put(
            f"/api/messaging/channels/{ch['id']}/assignments",
            json={"agent_ids": [a["id"]], "default_agent_id": b["id"]},
        )
        assert resp.status_code == 422


class TestLegacyScalar:
    async def test_the_scalar_stops_answering_once_the_join_speaks(
        self, messaging_client, seed_agent
    ):
        """A pre-join channel unassigned to empty must stay empty.

        The read-time fallback answers from the scalar when a channel has *no*
        assignment rows, so without retiring it, removing the last agent would
        resurrect the very one that was just removed.
        """
        from core.store import collections

        legacy, new = await _seed_agents("Legacy", "New")
        await collections.save(
            "messaging_channels",
            [{"id": "chn_old", "platform": "telegram", "agent_id": legacy["id"]}],
        )
        assert await channel_store.get_agent_ids_for_channel("chn_old") == [legacy["id"]]

        await messaging_client.put(
            "/api/messaging/channels/chn_old/assignments",
            json={"agent_ids": [new["id"]]},
        )
        await messaging_client.put(
            "/api/messaging/channels/chn_old/assignments", json={"agent_ids": []}
        )

        assert await channel_store.get_agent_ids_for_channel("chn_old") == []
        assert (await channel_store.get_channel("chn_old")).get("agent_id") is None
