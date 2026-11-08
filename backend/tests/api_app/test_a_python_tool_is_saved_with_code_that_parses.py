"""F1198 (BLD1): the tool editor's save checks a Python tool as the builder's does (`core.python_tool`).

`POST /api/tools/custom` took any document, so a Python tool with no code, or code that does not parse, was saved
and failed every call. Now both saves refuse it in the same words; an HTTP tool's save is as before; and a Python
tool saved before the check keeps loading and listing as it is (the check is on save only).
"""
from __future__ import annotations

import pytest

GOOD = {"name": "totals", "tool_type": "python", "description": "d", "code": "print(1)\n",
        "inputSchema": {"type": "object", "properties": {}}, "globals": []}


async def _listed(client, name):
    tools = (await client.get("/api/tools/custom")).json()
    assert isinstance(tools, list), tools
    return next((t for t in tools if t.get("name") == name), None)


async def test_a_good_python_tool_saves(client):
    resp = await client.post("/api/tools/custom", json=GOOD)
    assert resp.status_code == 200, resp.text


@pytest.mark.parametrize("code,words", [("", "cannot be empty"), ("def f(:\n", "does not parse: line 1")])
async def test_one_without_code_that_parses_is_refused(client, code, words):
    resp = await client.post("/api/tools/custom", json={**GOOD, "code": code})
    assert resp.status_code == 422 and words in resp.json()["detail"]
    assert await _listed(client, "totals") is None


async def test_a_bulk_save_names_the_one_refused_and_saves_none(client):
    resp = await client.post("/api/tools/custom/bulk", json={"tools": [
        {**GOOD, "name": "fine"}, {**GOOD, "name": "broken", "code": "if:\n"}]})
    assert resp.status_code == 422 and resp.json()["detail"].startswith("`broken`:")
    assert await _listed(client, "fine") is None


async def test_an_http_tools_save_is_as_before(client):
    resp = await client.post("/api/tools/custom", json={"name": "ping", "tool_type": "http", "method": "GET",
                                                        "url": "https://api.example.com/ping"})
    assert resp.status_code == 200, resp.text


async def test_one_saved_before_still_loads_and_lists(client):
    from core.store.resources import save_tool
    await save_tool({**GOOD, "name": "legacy", "code": "print('unclosed\n"})  # saved before the check, as it was
    assert (await _listed(client, "legacy")) is not None
