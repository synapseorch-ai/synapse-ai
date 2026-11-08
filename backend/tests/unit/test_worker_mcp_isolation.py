"""One failing MCP server must not take the servers listed after it.

This is backlog 10a-iv, and it was a real defect: both connect loops in
`worker_server_module.py` shared a single long-lived `AsyncExitStack`, so a
client context manager that blew up inside it left the stack unusable and every
server *after* the failing one in `resolve_mcp_servers()` order was lost too.
The outcome therefore depended on the order the resolver happened to return,
which is not a property worth having.

`e31e8ab` fixed it by giving each server its own task and its own stack
(`_ServerHandle` / `_own_server`). That was measured by hand against two real
servers and then nothing pinned it, which is what this file is for: the fix is
invisible to every other test, so a refactor that reintroduced one shared stack
would pass the whole suite.

The property under test is *order independence*, so every case runs both ways
round. A test that only ever put the healthy server first would pass against the
original bug as well — that was the order that always worked.

No network and no subprocess: the failure is injected at `_open_remote`, the
transport boundary, which is where the real one arrived from.
"""
import asyncio

import pytest

from core.scale import worker_server_module as wsm


class _Tool:
    def __init__(self, name):
        self.name = name


class _ToolsResult:
    def __init__(self, names):
        self.tools = [_Tool(n) for n in names]


class _FakeSession:
    """Enough of `ClientSession` for the connect loop, as an async CM."""

    def __init__(self, *_a, **_kw):
        self.initialised = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def initialize(self):
        self.initialised = True

    async def list_tools(self):
        return _ToolsResult(["do_thing"])


def _cfg(name: str, *, healthy: bool) -> dict:
    """A remote MCP server config in the shape `resolve_mcp_servers()` returns.

    No refresh material, so the OAuth retry in the connect loop stays out of
    this test — a refused refresh is `test_mcp_oauth.py`'s subject.
    """
    return {
        "name": name,
        "label": name,
        "server_type": "remote",
        "enabled": True,
        "url": f"https://{'good' if healthy else 'bad'}.example.com/mcp",
    }


@pytest.fixture
def transport(monkeypatch):
    """`_open_remote` succeeds for good.example.com and fails for bad.example.com.

    `CancelledError` is the failure deliberately: it is what an MCP client's
    `anyio` task group actually delivers when it loses a child to a `401`, it is
    a `BaseException` rather than an `Exception` (which is why `except
    Exception` never caught the original), and `_reason()` names it specially.
    """
    opened: list[str] = []

    async def fake_open_remote(exit_stack, url, headers):
        opened.append(url)
        if "bad." in url:
            raise asyncio.CancelledError()
        return ("read", "write")

    monkeypatch.setattr(wsm, "_open_remote", fake_open_remote)
    monkeypatch.setattr("mcp.ClientSession", _FakeSession)
    return opened


async def _connect(order: list[dict], monkeypatch) -> wsm.WorkerServerModule:
    async def fake_resolve():
        return order

    monkeypatch.setattr("core.scale.context.resolve_mcp_servers", fake_resolve)
    module = wsm.WorkerServerModule()
    await module._connect_user_mcp(set())  # noqa: SLF001 — the unit under test
    return module


class TestAFailingServerIsContainedToItself:
    @pytest.mark.parametrize(
        "order, label",
        [
            (["good", "bad"], "healthy first"),
            (["bad", "good"], "failing first"),
        ],
    )
    async def test_the_healthy_server_connects_whichever_order(
        self, order, label, transport, monkeypatch
    ):
        configs = [_cfg(n, healthy=(n == "good")) for n in order]
        module = await _connect(configs, monkeypatch)

        assert "ext_mcp_good" in module.agent_sessions, (
            f"the healthy server was lost with {label} — a failing server is "
            "taking down its neighbours again (10a-iv)"
        )
        assert module.agent_sessions["ext_mcp_good"].initialised
        assert module.mcp_disabled == ["bad"]
        assert module.tool_router.resolve("good__do_thing") is not None

        await module.close()

    async def test_both_orders_reach_the_same_outcome(self, transport, monkeypatch):
        """The point of the fix, stated as one assertion.

        Before `e31e8ab` these two disagreed: healthy-first connected the good
        server and skipped the bad one, bad-first lost both.
        """
        results = []
        for order in (["good", "bad"], ["bad", "good"]):
            configs = [_cfg(n, healthy=(n == "good")) for n in order]
            module = await _connect(configs, monkeypatch)
            results.append(
                (sorted(module.agent_sessions), sorted(module.mcp_disabled))
            )
            await module.close()

        assert results[0] == results[1], (
            "the outcome depends on resolve_mcp_servers() order again"
        )

    async def test_every_server_gets_its_own_stack(self, transport, monkeypatch):
        """Both servers are attempted, rather than the loop dying at the first.

        `_open_remote` recording two URLs is what separates "the second server
        was skipped" from "the second server was never reached", which is the
        distinction the shared stack destroyed.
        """
        configs = [_cfg(n, healthy=(n == "good")) for n in ("bad", "good")]
        module = await _connect(configs, monkeypatch)

        assert len(transport) == 2, f"only reached {transport}"
        await module.close()
