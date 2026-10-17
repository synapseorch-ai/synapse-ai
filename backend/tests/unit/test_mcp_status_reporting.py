"""What the worker records about a server it could not open.

`reauth_needed` has been in both UIs' vocabulary since their MCP screens were
written, and until now **nothing in a worker could set it**. A server whose
OAuth token had expired went on displaying `connected` while every agent using
it reported, accurately, that it had no such tool — the screen contradicting the
product, which is the worst pair of signals to hand someone.

The status is also worth being *exact* about, because it is an instruction to go
and click Authorize. Only two situations mean that: the refresh was attempted
and refused, or there was no refresh material to attempt with. A server that
accepted a freshly minted token and still failed is the server being down, and
telling the person to re-authorise sends them somewhere useless.
"""
import asyncio

import pytest

from core.scale import context, worker_server_module as wsm


class _Tool:
    def __init__(self, name):
        self.name = name


class _ToolsResult:
    def __init__(self, names):
        self.tools = [_Tool(n) for n in names]


class _FakeSession:
    def __init__(self, *_a, **_kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def initialize(self):
        return None

    async def list_tools(self):
        return _ToolsResult(["do_thing"])


def _oauth_cfg(name="vercel", *, url="https://bad.example.com/mcp"):
    return {
        "name": name,
        "label": name,
        "server_type": "remote",
        "enabled": True,
        "auth": "oauth",
        "url": url,
    }


@pytest.fixture
def reported(monkeypatch):
    """Capture what the connect loop reports, and stub the transport."""
    seen: list[tuple[str, str]] = []

    async def fake_report(server_name, status):
        seen.append((server_name, status))

    async def fake_open_remote(exit_stack, url, headers):
        if "bad." in url:
            raise asyncio.CancelledError()
        return ("read", "write")

    monkeypatch.setattr(context, "report_mcp_status", fake_report)
    monkeypatch.setattr(wsm, "_open_remote", fake_open_remote)
    monkeypatch.setattr("mcp.ClientSession", _FakeSession)
    return seen


async def _connect(cfgs, monkeypatch):
    async def fake_resolve():
        return cfgs

    monkeypatch.setattr("core.scale.context.resolve_mcp_servers", fake_resolve)
    module = wsm.WorkerServerModule()
    await module._connect_user_mcp(set())  # noqa: SLF001
    return module


def _oauth(monkeypatch, *, can, refresh_result):
    import core.mcp_oauth as mcp_oauth

    async def fake_refresh(cfg):
        return refresh_result

    monkeypatch.setattr(mcp_oauth, "can_refresh", lambda cfg: can)
    monkeypatch.setattr(mcp_oauth, "refresh_and_persist", fake_refresh)


class TestWhenReauthIsReported:
    async def test_a_refused_refresh_is_reported(self, reported, monkeypatch):
        _oauth(monkeypatch, can=True, refresh_result=None)
        module = await _connect([_oauth_cfg()], monkeypatch)

        assert ("vercel", "reauth_needed") in reported
        assert module.mcp_reauth_needed == ["vercel"]
        await module.close()

    async def test_a_server_with_nothing_to_refresh_with_is_reported(
        self, reported, monkeypatch
    ):
        """Authorised before the refresh fields were persisted, or a token that
        never had a refresh flow. Only the person can fix it."""
        _oauth(monkeypatch, can=False, refresh_result=None)
        module = await _connect([_oauth_cfg()], monkeypatch)

        assert ("vercel", "reauth_needed") in reported
        await module.close()


class TestWhenItIsNot:
    async def test_a_fresh_token_that_still_fails_is_not_a_reauth(
        self, reported, monkeypatch
    ):
        """The precision that matters. The refresh worked, so the credential is
        good and the server is simply unreachable — telling someone to
        re-authorise over a DNS blip is worse than saying nothing."""
        _oauth(monkeypatch, can=True, refresh_result="fresh-token")
        module = await _connect([_oauth_cfg()], monkeypatch)

        assert ("vercel", "reauth_needed") not in reported
        assert module.mcp_reauth_needed == []
        assert module.mcp_disabled == ["vercel"]
        await module.close()

    async def test_a_healthy_server_reports_connected(self, reported, monkeypatch):
        """So a server flagged once does not stay flagged after it is fixed."""
        _oauth(monkeypatch, can=True, refresh_result=None)
        module = await _connect(
            [_oauth_cfg(url="https://good.example.com/mcp")], monkeypatch
        )

        assert ("vercel", "connected") in reported
        await module.close()

    async def test_a_non_oauth_server_is_never_a_reauth(self, reported, monkeypatch):
        """A bearer-token server has no Authorize button to send anyone to."""
        _oauth(monkeypatch, can=False, refresh_result=None)
        cfg = {**_oauth_cfg(name="deepwiki"), "auth": "none"}
        module = await _connect([cfg], monkeypatch)

        assert ("deepwiki", "reauth_needed") not in reported
        await module.close()


class TestReportingNeverBreaksAJob:
    """A status is a courtesy to whoever looks at the screen later. Failing a
    tenant's MCP build over one would trade a cosmetic problem for a real one."""

    async def test_a_provider_without_the_method_is_fine(self, monkeypatch):
        class _ReadOnlyProvider:
            async def resolve_agent(self, agent_id): return None
            async def resolve_orchestration(self, orch_id): return None
            async def resolve_custom_tools(self): return []
            async def resolve_mcp_servers(self): return []

        monkeypatch.setattr(context, "_provider", _ReadOnlyProvider())
        await context.report_mcp_status("vercel", "reauth_needed")  # must not raise

    async def test_a_provider_that_raises_is_swallowed(self, monkeypatch, capsys):
        class _AngryProvider:
            async def resolve_agent(self, agent_id): return None
            async def resolve_orchestration(self, orch_id): return None
            async def resolve_custom_tools(self): return []
            async def resolve_mcp_servers(self): return []

            async def report_mcp_status(self, server_name, status):
                raise RuntimeError("postgres is away")

        monkeypatch.setattr(context, "_provider", _AngryProvider())
        await context.report_mcp_status("vercel", "reauth_needed")

        assert "could not record MCP status" in capsys.readouterr().out


class TestTheLiveToolListIsReported:
    """F524: a run lists a connected server's tools live, and that list went nowhere — a host that stores a
    server's tools (cloud's discovery) kept counting the list it saw when the server was added. The connect hands
    the live list to the host now, beside its status."""

    async def test_a_connect_reports_the_tools_it_listed(self, reported, monkeypatch):
        tools: list[tuple[str, list[dict]]] = []

        async def fake_tools(server_name, listed):
            tools.append((server_name, listed))

        monkeypatch.setattr(context, "report_mcp_tools", fake_tools)
        _oauth(monkeypatch, can=True, refresh_result=None)
        module = await _connect([_oauth_cfg(url="https://good.example.com/mcp")], monkeypatch)

        assert tools == [("vercel", [{"name": "do_thing", "description": "", "inputSchema": {}}])]
        await module.close()

    async def test_what_each_tool_says_it_does_is_reported_too(self, reported, monkeypatch):
        """TAG1 (F740): the stored list kept no annotations, so a host's screens and badges said every server tool
        was undeclared while the run read its hints live."""
        from mcp.types import ToolAnnotations

        tools: list[tuple[str, list[dict]]] = []

        async def fake_tools(server_name, listed):
            tools.append((server_name, listed))

        async def list_tools(self):
            result = _ToolsResult(["do_thing"])
            told = _Tool("deploy")
            told.annotations = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True,
                                               title="Deploy")
            result.tools.append(told)
            return result

        monkeypatch.setattr(_FakeSession, "list_tools", list_tools)
        monkeypatch.setattr(context, "report_mcp_tools", fake_tools)
        _oauth(monkeypatch, can=True, refresh_result=None)
        module = await _connect([_oauth_cfg(url="https://good.example.com/mcp")], monkeypatch)

        assert tools == [("vercel", [
            {"name": "do_thing", "description": "", "inputSchema": {}},
            {"name": "deploy", "description": "", "inputSchema": {},
             "annotations": {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True}},
        ])]
        await module.close()

    async def test_a_server_that_did_not_connect_reports_no_list(self, reported, monkeypatch):
        tools: list = []

        async def fake_tools(server_name, listed):
            tools.append(server_name)

        monkeypatch.setattr(context, "report_mcp_tools", fake_tools)
        _oauth(monkeypatch, can=True, refresh_result=None)
        module = await _connect([_oauth_cfg()], monkeypatch)

        assert tools == []
        await module.close()

    async def test_without_a_host_nothing_is_stored(self, monkeypatch):
        """Self-hosted lists its servers' tools live everywhere and stores none: nothing to keep fresh."""
        monkeypatch.setattr(context, "_provider", None)
        await context.report_mcp_tools("vercel", [{"name": "x", "description": "", "inputSchema": {}}])

    async def test_a_host_without_the_method_or_that_raises_never_breaks_the_build(self, monkeypatch, capsys):
        class _Old:
            async def resolve_mcp_servers(self): return []

        monkeypatch.setattr(context, "_provider", _Old())
        await context.report_mcp_tools("vercel", [])

        class _Angry(_Old):
            async def report_mcp_tools(self, server_name, tools):
                raise RuntimeError("postgres is away")

        monkeypatch.setattr(context, "_provider", _Angry())
        await context.report_mcp_tools("vercel", [])
        assert "could not record MCP tools" in capsys.readouterr().out
