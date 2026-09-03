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
