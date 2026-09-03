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
