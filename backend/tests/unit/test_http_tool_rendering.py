"""An HTTP custom tool renders `{{global.*}}` and `{{arg}}` into its URL and
headers, and sends the body its template names — through the real
`run_agent_step` loop, with the outbound request captured at httpx."""

import httpx
import pytest

from core import variables
from core.store.resources import save_tools
from _fakes import seed as S
from _fakes.fake_llm import tool_call
from test_react_engine_deep import _drive


class _Captured:
    def __init__(self):
        self.calls: list[dict] = []


@pytest.fixture
def outbound(monkeypatch):
    captured = _Captured()

    async def fake_request(self, method, url, **kwargs):
        captured.calls.append({"method": method, "url": url, **kwargs})
        return httpx.Response(200, json={"ok": True, "echo": kwargs.get("json")})

    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)
    return captured


def _schema(name):
    return {"type": "function", "function": {"name": name, "description": "t", "parameters": {
        "type": "object", "properties": {"id": {"type": "integer"}, "body": {"type": "object"}, "note": {"type": "string"}}}}}


async def _tool(**overrides):
    tool = {
        "name": "post_thing", "description": "t", "tool_type": "http", "method": "POST",
        "url": "{{global.api_base}}/things/{{id}}",
        "headers": {"X-Key": "{{global.key}}", "Accept": "application/json"},
        "inputSchema": _schema("post_thing")["function"]["parameters"],
        **overrides,
    }
    await save_tools([tool])
    return tool


class TestRendering:
    async def test_globals_and_args_reach_the_url_and_headers_and_body_is_the_named_argument(self, fake_llm, outbound):
        await variables.save({"api_base": "https://api.test", "key": "k1"})
        await _tool(body="{{body}}")
        agent = S.make_agent(tools=["post_thing"], skip_default_tools=True)

        await _drive(agent, fake_llm, [tool_call("post_thing", id=7, body={"a": 1}, note="n"), "done"],
                     tools_override=[_schema("post_thing")], tool_executor=None)

        assert len(outbound.calls) == 1
        call = outbound.calls[0]
        assert call["method"] == "POST"
        assert call["url"] == "https://api.test/things/7"
        assert call["headers"]["X-Key"] == "k1"
        assert call["json"] == {"a": 1}, "the `body` argument is the body — nothing else is"

    async def test_without_a_body_template_the_arguments_minus_the_url_ones_are_the_body(self, fake_llm, outbound):
        await variables.save({"api_base": "https://api.test", "key": "k1"})
        await _tool()
        agent = S.make_agent(tools=["post_thing"], skip_default_tools=True)
        await _drive(agent, fake_llm, [tool_call("post_thing", id=7, body={"a": 1}, note="n"), "done"],
                     tools_override=[_schema("post_thing")], tool_executor=None)
        assert outbound.calls[0]["json"] == {"body": {"a": 1}, "note": "n"}, "id went into the URL, so it is not sent twice"

    async def test_a_json_body_template_shapes_the_body(self, fake_llm, outbound):
        await variables.save({"api_base": "https://api.test", "key": "k1", "source": "synapse"})
        await _tool(body='{"customer": {{body}}, "ref": "{{id}}", "via": "{{global.source}}"}')
        agent = S.make_agent(tools=["post_thing"], skip_default_tools=True)
        await _drive(agent, fake_llm, [tool_call("post_thing", id=7, body={"a": 1}, note="n"), "done"],
                     tools_override=[_schema("post_thing")], tool_executor=None)
        assert outbound.calls[0]["json"] == {"customer": {"a": 1}, "ref": 7, "via": "synapse"}

    async def test_an_unset_global_stays_visible_in_the_url(self, fake_llm, outbound):
        await _tool(url="{{global.nope}}/x")
        agent = S.make_agent(tools=["post_thing"], skip_default_tools=True)
        await _drive(agent, fake_llm, [tool_call("post_thing", id=1), "done"],
                     tools_override=[_schema("post_thing")], tool_executor=None)
        assert outbound.calls[0]["url"] == "{{global.nope}}/x"

    async def test_get_sends_the_rest_as_query_params(self, fake_llm, outbound):
        await variables.save({"api_base": "https://api.test"})
        await _tool(method="GET", url="{{global.api_base}}/things/{{id}}", body="")
        agent = S.make_agent(tools=["post_thing"], skip_default_tools=True)
        await _drive(agent, fake_llm, [tool_call("post_thing", id=7, note="n"), "done"],
                     tools_override=[_schema("post_thing")], tool_executor=None)
        call = outbound.calls[0]
        assert call["method"] == "GET" and call["url"] == "https://api.test/things/7"
        assert call["params"] == {"note": "n"}


class TestTheToolStepMatchesTheAgent:
    """The same tool, called by an orchestration TOOL step.

    This is the half that had none of D69's three fixes. `steps.py` carried its
    own copy of the HTTP executor, so a tool called from a TOOL step sent a
    literal `{{global.api_base}}` to the server, ignored its `body` template
    entirely, and put arguments in the body that the URL had already consumed —
    while the identical tool called by an agent did all three correctly.

    Both build their request through `core.http_tool` now. These assertions are
    deliberately the same ones made above, against the other caller.
    """

    async def _call(self, args):
        from core.orchestration.steps import ToolStepExecutor
        from core.store.resources import load_tools

        tool = next(t for t in await load_tools() if t["name"] == "post_thing")
        return await ToolStepExecutor()._execute_http_tool(tool, args)

    async def test_globals_and_args_reach_the_url_and_headers(self, outbound):
        await variables.save({"api_base": "https://api.test", "key": "k1"})
        await _tool(body="{{body}}")

        await self._call({"id": 7, "body": {"a": 1}, "note": "n"})

        call = outbound.calls[0]
        assert call["url"] == "https://api.test/things/7"
        assert call["headers"]["X-Key"] == "k1"
        assert call["json"] == {"a": 1}, "the body template is honoured here too"

    async def test_a_url_argument_is_not_sent_twice(self, outbound):
        """The bug the old copy's own comment described:
        'POST / PUT - all args in body even if some were used in the URL'."""
        await variables.save({"api_base": "https://api.test", "key": "k1"})
        await _tool()

        await self._call({"id": 7, "body": {"a": 1}, "note": "n"})

        assert outbound.calls[0]["json"] == {"body": {"a": 1}, "note": "n"}
        assert "id" not in outbound.calls[0]["json"]

    async def test_get_sends_the_rest_as_query_params(self, outbound):
        await variables.save({"api_base": "https://api.test"})
        await _tool(method="GET", body="")

        await self._call({"id": 7, "note": "n"})

        call = outbound.calls[0]
        assert call["method"] == "GET" and call["url"] == "https://api.test/things/7"
        assert call["params"] == {"note": "n"}


class TestSingleBraceStillWorks:
    """Every URL written before `{{ … }}` existed, and every OpenAPI import.

    OpenAPI's own path templating is single-brace — `openapi_import.py` builds
    the URL as `base + path` straight from the spec — so `{petId}` is not a
    legacy spelling anybody chose. It arrives that way.
    """

    async def test_a_single_brace_argument_still_fills_and_is_consumed(self, outbound):
        await _tool(url="https://api.test/things/{id}", body="")
        await TestTheToolStepMatchesTheAgent()._call({"id": 7, "note": "n"})
        call = outbound.calls[0]
        assert call["url"] == "https://api.test/things/7"
        assert call["json"] == {"note": "n"}, "consumed by the URL, so not in the body"

    async def test_the_two_syntaxes_mix_in_one_url(self, outbound):
        """`{{global.*}}` expands first, so it may expand *into* a `{param}`."""
        await variables.save({"api_base": "https://api.test"})
        await _tool(url="{{global.api_base}}/things/{id}", body="")
        await TestTheToolStepMatchesTheAgent()._call({"id": 9})
        assert outbound.calls[0]["url"] == "https://api.test/things/9"

    async def test_data_means_the_whole_payload_and_has_no_double_brace_spelling(self, outbound):
        await _tool(url="https://api.test/q?p={data}", body="")
        await TestTheToolStepMatchesTheAgent()._call({"a": 1})
        assert outbound.calls[0]["url"] == 'https://api.test/q?p={"a": 1}'

    async def test_an_unknown_single_brace_reference_is_left_alone(self, outbound):
        """Never blanked — a URL that loses a segment silently is worse than one
        that visibly still has a placeholder in it."""
        await _tool(url="https://api.test/{nope}", body="")
        await TestTheToolStepMatchesTheAgent()._call({"id": 1})
        assert outbound.calls[0]["url"] == "https://api.test/{nope}"


class TestEgressAtTheMomentOfTheCall:
    """S4: the URL a custom tool dials is checked when it is dialled.

    `core/egress_guard.py` has existed since the scraper needed it and the
    scraper was the only caller, so a tool pointed at the cloud metadata
    endpoint ran — from a worker that on a shared fleet holds someone's
    credentials. Checking at creation cannot cover it: the Tools screen takes a
    raw document, and DNS can be re-pointed after a tool is saved.
    """

    class _NeverCalled:
        async def request(self, **kwargs):
            raise AssertionError(f"the request was sent anyway: {kwargs['url']}")

    @pytest.mark.parametrize("url", [
        "http://169.254.169.254/latest/meta-data/iam/security-credentials/",
        "http://[fd00::1]/internal",
        "http://127.0.0.1:8000/admin",
        "http://localhost/admin",
        "http://10.0.0.5/internal",
        "http://192.168.1.1/",
        "http://100.64.0.1/",
        "http://0.0.0.0/",
    ])
    async def test_an_inward_url_never_reaches_the_client(self, url):
        from core import http_tool
        from core.egress_guard import BlockedEgress

        with pytest.raises(BlockedEgress):
            await http_tool.send(
                self._NeverCalled(), {"url": url, "method": "GET"}, {}, {}
            )

    async def test_a_non_http_scheme_is_refused(self):
        from core import http_tool
        from core.egress_guard import BlockedEgress

        with pytest.raises(BlockedEgress):
            await http_tool.send(
                self._NeverCalled(), {"url": "file:///etc/passwd", "method": "GET"}, {}, {}
            )

    async def test_the_check_sees_the_rendered_url_not_the_template(self):
        # The whole reason this cannot live in `build_request`: the address is
        # not known until the arguments are substituted in.
        from core import http_tool
        from core.egress_guard import BlockedEgress

        tool = {"url": "http://{{args.host}}/meta-data/", "method": "GET"}
        with pytest.raises(BlockedEgress):
            await http_tool.send(
                self._NeverCalled(), tool, {"host": "169.254.169.254"}, {}
            )

    async def test_allow_hosts_is_the_escape_hatch(self, monkeypatch):
        from core import http_tool

        sent = {}

        class _Recording:
            async def request(self, **kwargs):
                sent.update(kwargs)
                return "ok"

        monkeypatch.setenv("EGRESS_ALLOW_HOSTS", "internal.example.test,127.0.0.1")
        result = await http_tool.send(
            _Recording(), {"url": "http://127.0.0.1:9/ping", "method": "GET"}, {}, {}
        )
        assert result == "ok"
        assert sent["url"] == "http://127.0.0.1:9/ping"

    async def test_a_public_url_still_goes_out(self):
        from core import http_tool

        sent = {}

        class _Recording:
            async def request(self, **kwargs):
                sent.update(kwargs)
                return "ok"

        await http_tool.send(
            _Recording(), {"url": "https://example.com/api", "method": "GET"}, {}, {}
        )
        assert sent["url"] == "https://example.com/api"

    async def test_a_host_that_does_not_resolve_is_not_our_verdict_to_make(self):
        # The blast-radius case: an internal API, a service not yet deployed, a
        # split-horizon DNS. Blocking here would break every such tool on every
        # self-hosted install, and would buy nothing — httpx fails the same
        # lookup a moment later if the host really is absent.
        from core import http_tool

        sent = {}

        class _Recording:
            async def request(self, **kwargs):
                sent.update(kwargs)
                return "ok"

        await http_tool.send(
            _Recording(),
            {"url": "https://nothing-here.invalid/api", "method": "GET"}, {}, {},
        )
        assert sent["url"] == "https://nothing-here.invalid/api"


class TestTheToolStepSendsWhatTheTenantResolved:
    """F1: a TOOL step executed from the store's raw rows, not the resolved ones.

    On a hosted deployment a tool header is stored as a `synsec://` reference and
    the resource provider resolves it — the agent path and the TOOL step's own
    schema lookup both ask the provider. The call itself re-read the store, so the
    reference went to the server as the header's value.
    """

    async def test_the_header_is_the_resolved_value_not_the_reference(self, outbound):
        from types import SimpleNamespace

        from core.orchestration.steps import ToolStepExecutor
        from core.scale.context import set_resource_provider

        stored = {
            "name": "post_thing", "description": "t", "tool_type": "http", "method": "POST",
            "url": "https://api.test/things", "headers": {"Authorization": "synsec://tool:post_thing:authorization"},
            "inputSchema": {"type": "object", "properties": {}},
        }
        await save_tools([stored])

        class _Resolves:
            async def resolve_agent(self, agent_id):
                return None

            async def resolve_orchestration(self, orch_id):
                return None

            async def resolve_custom_tools(self):
                return [{**stored, "headers": {"Authorization": "Bearer resolved"}}]

            async def resolve_mcp_servers(self):
                return []

        engine = SimpleNamespace(server_module=SimpleNamespace(tool_router={}, agent_sessions={}))
        set_resource_provider(_Resolves())
        try:
            await ToolStepExecutor()._execute_tool("post_thing", {"note": "n"}, engine)
        finally:
            set_resource_provider(None)

        assert outbound.calls[0]["headers"]["Authorization"] == "Bearer resolved"


class TestARefusalSaysHowToAllowIt:
    """The shipped n8n URL is `http://localhost:5678`, so every n8n tool call was
    refused with "localhost resolves to a blocked range: loopback" and nothing
    said the refusal can be lifted (F78). A self-hosted install is told the
    setting; a multi-tenant deployment, whose customers cannot set environment
    variables, is not."""

    class _NeverCalled:
        async def request(self, **kwargs):
            raise AssertionError("the request was sent anyway")

    async def test_the_host_and_the_variable_are_named(self, monkeypatch):
        from core import http_tool
        from core.egress_guard import BlockedEgress
        monkeypatch.delenv("EGRESS_ALLOW_HOSTS", raising=False)

        with pytest.raises(BlockedEgress) as refused:
            await http_tool.send(self._NeverCalled(),
                                 {"url": "http://localhost:5678/webhook/x", "method": "POST"}, {}, {})

        assert "EGRESS_ALLOW_HOSTS" in str(refused.value)
        assert "localhost" in str(refused.value)
        assert "blocked range" in str(refused.value)

    async def test_not_where_nobody_can_set_it(self, monkeypatch):
        from core import http_tool, tenancy
        from core.egress_guard import BlockedEgress
        monkeypatch.delenv("EGRESS_ALLOW_HOSTS", raising=False)
        monkeypatch.setattr(tenancy, "_multi_tenant", True)

        with pytest.raises(BlockedEgress) as refused:
            await http_tool.send(self._NeverCalled(),
                                 {"url": "http://localhost:5678/webhook/x", "method": "POST"}, {}, {})

        assert "EGRESS_ALLOW_HOSTS" not in str(refused.value)

    async def test_a_scheme_refusal_names_no_variable(self):
        """No setting allows a file:// URL, so none is suggested."""
        from core import http_tool
        from core.egress_guard import BlockedEgress

        with pytest.raises(BlockedEgress) as refused:
            await http_tool.send(self._NeverCalled(), {"url": "file:///etc/passwd", "method": "GET"}, {}, {})

        assert "EGRESS_ALLOW_HOSTS" not in str(refused.value)
