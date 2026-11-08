"""A turn waits only for the MCP servers it can call, and they connect at once.

VOX-MCP (D262). The pool used to build a tenant's module with every server it
had, one after another, before any job could use it: acme's three servers took
~12 s, and a phone call's agent that used none of them waited for all three.

Now an entry is ready once its native servers are up, and each `acquire()`
connects the tenant's servers its turn can use (`mcp_pool.needing`) — all at
once — and waits for those alone. The properties a fleet depends on:

* a turn connects only what it needs, and a later turn adds its own without
  changing the map an earlier turn is iterating;
* a turn that needs nothing is never held by another turn's slow server;
* nothing said is every server, as before;
* one server is connected once, however many turns ask, and a failed one is not
  retried for each of them;
* an epoch's `invalidate()` during a connect leaks no session;
* connecting at once still keeps the listed order and the 10a-iv isolation.

No network: the transport is faked at `_open_remote`, where the real failures
arrive, as `test_worker_mcp_isolation.py` does.
"""
import asyncio

import pytest

from core.scale import mcp_pool
from core.scale import worker_server_module as wsm
from core.scale.context import set_resource_provider
from core.tenancy import tenant_scope


class _Tool:
    def __init__(self, name):
        self.name = name


class _ToolsResult:
    def __init__(self, names):
        self.tools = [_Tool(n) for n in names]


class _Session:
    """Enough of `ClientSession` for a connect, recording when it is closed."""

    closed: list[str] = []

    def __init__(self, read, *_a, **_kw):
        self.url = read

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        _Session.closed.append(self.url)
        return False

    async def initialize(self):
        return None

    async def list_tools(self):
        return _ToolsResult(["do_thing"])


def _cfg(name: str) -> dict:
    return {"name": name, "label": name, "server_type": "remote", "enabled": True,
            "url": f"https://{name}.example.com/mcp"}


class _Provider:
    def __init__(self):
        self.servers: list[dict] = []

    async def resolve_agent(self, agent_id):
        return None

    async def resolve_orchestration(self, orch_id):
        return None

    async def resolve_custom_tools(self):
        return []

    async def resolve_mcp_servers(self):
        return self.servers


@pytest.fixture
def net(monkeypatch):
    """The transport: per-server delay and failure, and every URL opened.

    `gate` (a `Together`) makes connects that must overlap meet first; `held[name]`
    keeps that server's connect open until the test sets it; `entered[name]` is set
    once its connect has begun. Events, not time windows (F519).
    """
    state = {"delay": {}, "fail": set(), "opened": [], "gate": None, "held": {}, "entered": {}}

    async def fake_open_remote(exit_stack, url, headers):
        state["opened"].append(url)
        name = url.split("//", 1)[1].split(".", 1)[0]
        state["entered"].setdefault(name, asyncio.Event()).set()
        if state["gate"] is not None:
            await state["gate"].arrive()
        if name in state["held"]:
            await state["held"][name].wait()
        await asyncio.sleep(state["delay"].get(name, 0))
        if name in state["fail"]:
            raise asyncio.CancelledError()  # what a 401 arrives as
        return (url, "write")

    _Session.closed = []
    monkeypatch.setattr(wsm, "_open_remote", fake_open_remote)
    monkeypatch.setattr("mcp.ClientSession", _Session)

    async def no_status(server_name, status):
        return None

    monkeypatch.setattr("core.scale.context.report_mcp_status", no_status)
    return state


def _opened(net, name):
    return sum(1 for url in net["opened"] if url.startswith(f"https://{name}."))


@pytest.fixture
async def pool(monkeypatch, net):
    """A real module per tenant, built as the pool builds it, minus the native
    subprocesses (one stand-in native so the module is never empty)."""
    provider = _Provider()
    set_resource_provider(provider)

    async def build(tenant):
        with tenant_scope(tenant):
            module = wsm.WorkerServerModule()
            module.agent_sessions = {"time": object()}
            await module._resolve_user_mcp(set())  # noqa: SLF001 — `connect_user=False`
            return module

    mcp_pool.reset_state()
    monkeypatch.setattr(mcp_pool, "_build", build)
    yield provider
    await mcp_pool.drain()
    mcp_pool.reset_state()
    set_resource_provider(None)


async def _acquire(needs="unset"):
    if needs == "unset":
        return await mcp_pool.acquire()
    with mcp_pool.needing(needs):
        return await mcp_pool.acquire()


class TestATurnConnectsWhatItNeeds:
    async def test_only_the_named_server_is_connected(self, pool, net):
        pool.servers = [_cfg("alpha"), _cfg("beta"), _cfg("gamma")]
        with tenant_scope("acme"):
            module = await _acquire({"beta__do_thing", "web_search"})
            await mcp_pool.release()

        assert list(module.agent_sessions) == ["time", "ext_mcp_beta"]
        assert net["opened"] == ["https://beta.example.com/mcp"]
        assert module.tool_router.resolve("beta__do_thing") is not None

    async def test_a_later_turn_adds_its_own_without_changing_the_earlier_ones_map(self, pool, net):
        pool.servers = [_cfg("alpha"), _cfg("beta")]
        with tenant_scope("acme"):
            first = await _acquire({"beta__do_thing"})
            held = first.agent_sessions  # what an iterating turn holds
            second = await _acquire({"alpha__do_thing"})
            await mcp_pool.release()
            await mcp_pool.release()

        assert second is first, "one entry per org"
        assert list(held) == ["time", "ext_mcp_beta"], "the earlier turn's map changed under it"
        # The listed order, though beta answered first.
        assert list(first.agent_sessions) == ["time", "ext_mcp_alpha", "ext_mcp_beta"]
        assert set(first._session_tools) >= {"ext_mcp_alpha", "ext_mcp_beta"}  # noqa: SLF001

    async def test_nothing_said_is_every_server(self, pool, net):
        pool.servers = [_cfg("alpha"), _cfg("beta")]
        with tenant_scope("acme"):
            module = await mcp_pool.acquire()
            await mcp_pool.release()

        assert list(module.agent_sessions) == ["time", "ext_mcp_alpha", "ext_mcp_beta"]

    async def test_a_turn_that_needs_nothing_is_not_held_by_a_slow_server(self, pool, net):
        from _fakes.together import GIVE_UP
        pool.servers = [_cfg("slow")]
        held = net["held"]["slow"] = asyncio.Event()  # the slow server connects only when let
        entered = net["entered"].setdefault("slow", asyncio.Event())
        with tenant_scope("acme"):
            everything = asyncio.create_task(_acquire(None))
            await asyncio.wait_for(entered.wait(), GIVE_UP)  # the slow connect is under way
            # Answered while the slow server is still connecting: it never waited for one.
            module = await asyncio.wait_for(_acquire(frozenset()), GIVE_UP)
            assert "ext_mcp_slow" not in module.agent_sessions
            assert not everything.done()
            held.set()
            await everything
            await mcp_pool.release()
            await mcp_pool.release()

        assert "ext_mcp_slow" in module.agent_sessions, "and the warm-up still finished"

    async def test_two_turns_needing_one_server_connect_it_once(self, pool, net):
        pool.servers = [_cfg("alpha")]
        net["delay"]["alpha"] = 0.1
        with tenant_scope("acme"):
            a, b = await asyncio.gather(_acquire({"alpha__do_thing"}), _acquire({"alpha__do_thing"}))
            await mcp_pool.release()
            await mcp_pool.release()

        assert a is b
        assert _opened(net, "alpha") == 1

    async def test_a_failed_server_is_not_tried_again_for_every_turn(self, pool, net):
        pool.servers = [_cfg("bad")]
        net["fail"].add("bad")
        with tenant_scope("acme"):
            module = await _acquire({"bad__do_thing"})
            await mcp_pool.release()
            await _acquire({"bad__do_thing"})
            await mcp_pool.release()

        assert module.mcp_disabled == ["bad"]
        assert _opened(net, "bad") == 1


class TestTheWaitIsBounded:
    async def test_a_server_past_the_bound_fails_the_turn_names_the_tenant_and_is_stopped(
        self, pool, net, monkeypatch
    ):
        from _fakes.together import GIVE_UP
        pool.servers = [_cfg("stuck")]
        net["held"]["stuck"] = asyncio.Event()  # never set: it never connects
        monkeypatch.setattr(mcp_pool, "_build_timeout", lambda: 0.2)
        with tenant_scope("acme"):
            with pytest.raises(TimeoutError, match="stuck.*'acme'"):
                await _acquire({"stuck__do_thing"})
            # Stopped, not inherited: the next turn is told at once — it would wait for
            # ever on a connect that never ends if it had inherited the stopped one (F519).
            module = await asyncio.wait_for(_acquire({"stuck__do_thing"}), GIVE_UP)
            await mcp_pool.release()

        assert "ext_mcp_stuck" not in module.agent_sessions
        assert mcp_pool.stats()["tenants"]["acme"]["refs"] == 0, "the failed acquire kept its ref"


class TestAnEpochDuringAConnect:
    async def test_the_late_session_is_closed_and_the_turn_gets_a_fresh_module(self, pool, net):
        pool.servers = [_cfg("slow")]
        net["delay"]["slow"] = 0.3
        with tenant_scope("acme"):
            turn = asyncio.create_task(_acquire({"slow__do_thing"}))
            await asyncio.sleep(0.05)
            old = next(iter(mcp_pool._entries.values())).module  # noqa: SLF001
            await mcp_pool.invalidate("acme")  # an MCP edit, mid-connect
            module = await turn
            await mcp_pool.release()
            await asyncio.sleep(0.4)  # the old connect finishes opening, then closes

        assert old.closed
        assert "ext_mcp_slow" not in old.agent_sessions, "a closed module took a session"
        assert module is not old
        assert "ext_mcp_slow" in module.agent_sessions
        assert _Session.closed == ["https://slow.example.com/mcp"], (
            "the session opened for the closed module was never closed"
        )


class TestConnectingAtOnce:
    @pytest.mark.parametrize("order", [["slow", "good", "bad"], ["bad", "good", "slow"]])
    async def test_the_slowest_sets_the_pace_and_the_list_sets_the_order(self, net, monkeypatch, order):
        from _fakes.together import Together
        net["delay"].update({"slow": 0.4, "bad": 0.3})
        net["fail"].add("bad")
        gate = net["gate"] = Together(3)

        async def resolve():
            return [_cfg(n) for n in order]

        monkeypatch.setattr("core.scale.context.resolve_mcp_servers", resolve)
        module = wsm.WorkerServerModule()
        await module._connect_user_mcp(set())  # noqa: SLF001 — every server, as before

        assert gate.held, "the servers connected one after another"
        assert module.mcp_disabled == ["bad"]
        connected = [n for n in order if n != "bad"]
        assert list(module.agent_sessions) == [f"ext_mcp_{n}" for n in connected]
        await module.close()

    async def test_disabled_and_reauth_lists_keep_the_listed_order(self, net, monkeypatch):
        """Whichever finishes first, the lists read in the order the servers are listed."""
        net["fail"].update({"first", "second"})
        net["delay"].update({"first": 0.2})

        async def resolve():
            return [_cfg("first"), _cfg("second")]

        monkeypatch.setattr("core.scale.context.resolve_mcp_servers", resolve)
        module = wsm.WorkerServerModule()
        await module._connect_user_mcp(set())  # noqa: SLF001
        assert module.mcp_disabled == ["first", "second"]

    async def test_close_stops_a_connect_under_way(self, net, monkeypatch):
        net["delay"]["slow"] = 0.3

        async def resolve():
            return [_cfg("slow")]

        monkeypatch.setattr("core.scale.context.resolve_mcp_servers", resolve)
        module = wsm.WorkerServerModule()
        ensuring = asyncio.create_task(module.ensure(None))
        await asyncio.sleep(0.05)
        await module.close()
        await ensuring
        await asyncio.sleep(0.4)

        assert module.closed
        assert "ext_mcp_slow" not in module.agent_sessions
        assert _Session.closed == ["https://slow.example.com/mcp"]


class TestThePoolBuildsWithoutTheTenantsServers:
    async def test_build_asks_for_the_natives_only(self, monkeypatch):
        seen = {}

        async def build_for_tenant(shared=None, disabled_mcp_names=None, *, connect_user=True):
            seen["connect_user"] = connect_user
            return object()

        monkeypatch.setattr(wsm.WorkerServerModule, "build_for_tenant", staticmethod(build_for_tenant))
        await mcp_pool._build("acme")  # noqa: SLF001
        assert seen == {"connect_user": False}

    def test_the_whole_build_is_still_the_default(self):
        import inspect

        default = inspect.signature(wsm.WorkerServerModule.build_for_tenant).parameters["connect_user"].default
        assert default is True
