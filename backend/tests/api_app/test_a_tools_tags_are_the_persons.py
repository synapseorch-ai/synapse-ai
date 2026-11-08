"""A custom tool's tags come from its request, stay the person's, and arrive from a file as a claim (TAG1, D296, F734).

- The OpenAPI preview shows each operation's tags, so the person sees them before saving.
- Importing the same spec again keeps the tags a person set on a tool whose method has not changed.
- A bundle's tags were saved as written, so a file could say "read-only" and be believed. They are marked as
  arrived from a file (`annotations_by: "import"`), believed when they restrict, until a person saves the tool.
"""

from __future__ import annotations

import json
from typing import Any

from core.tool_policy import decide
from core.tool_tags import defaults_for, hints_of

SPEC = {
    "openapi": "3.0.0",
    "info": {"title": "Bookings"},
    "servers": [{"url": "https://api.example.com"}],
    "paths": {
        "/bookings": {
            "get": {"operationId": "list_bookings", "summary": "List bookings"},
            "post": {"operationId": "create_booking", "summary": "Create a booking"},
        },
        "/bookings/{id}": {
            "put": {"operationId": "replace_booking", "summary": "Replace a booking",
                    "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}]},
            "delete": {"operationId": "cancel_booking", "summary": "Cancel a booking",
                       "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}]},
        },
    },
}


async def _tools(client: Any) -> dict[str, dict]:
    return {t["name"]: t for t in (await client.get("/api/tools/custom")).json()}


async def test_the_preview_shows_each_operations_tags(client: Any) -> None:
    r = await client.post("/api/tools/import/openapi", json={"spec": json.dumps(SPEC)})
    assert r.status_code == 200, r.text
    got = {t["name"]: (t["method"], t["annotations"]) for t in r.json()["tools"]}
    assert got == {name: (method, hints_of(defaults_for(method))) for name, method in (
        ("list_bookings", "GET"), ("create_booking", "POST"),
        ("replace_booking", "PUT"), ("cancel_booking", "DELETE"))}
    assert "create_booking" not in await _tools(client), "a preview saves nothing"


async def test_importing_again_keeps_the_persons_tags(client: Any) -> None:
    r = await client.post("/api/tools/import/openapi", json={"spec": json.dumps(SPEC), "save": True})
    assert r.status_code == 200, r.text
    tool = (await _tools(client))["create_booking"]
    # The person says this POST is an upsert by key: safe to repeat, and it stays inside.
    mine = {**hints_of(defaults_for("POST")), "idempotentHint": True, "openWorldHint": False}
    r = await client.post("/api/tools/custom", json={**tool, "annotations": mine})
    assert r.status_code == 200, r.text

    for body in ({"spec": json.dumps(SPEC)}, {"spec": json.dumps(SPEC), "save": True}):
        r = await client.post("/api/tools/import/openapi", json=body)
        assert {t["name"]: t["annotations"] for t in r.json()["tools"]}["create_booking"] == mine
    assert (await _tools(client))["create_booking"]["annotations"] == mine


async def test_a_changed_method_takes_its_new_defaults(client: Any) -> None:
    await client.post("/api/tools/import/openapi", json={"spec": json.dumps(SPEC), "save": True})
    tool = (await _tools(client))["list_bookings"]
    await client.post("/api/tools/custom", json={**tool, "annotations": {**tool["annotations"], "openWorldHint": False}})
    changed = json.loads(json.dumps(SPEC))
    changed["paths"]["/bookings"]["post"]["operationId"] = "x_post"
    changed["paths"]["/bookings"]["delete"] = changed["paths"]["/bookings"].pop("get")
    changed["paths"]["/bookings"]["delete"]["operationId"] = "list_bookings"
    r = await client.post("/api/tools/import/openapi", json={"spec": json.dumps(changed)})
    got = {t["name"]: t["annotations"] for t in r.json()["tools"]}
    assert got["list_bookings"] == hints_of(defaults_for("DELETE"))


async def test_a_bundles_tags_are_a_claim_until_a_person_saves(client: Any) -> None:
    claims_to_read = {"name": "pay_invoice", "description": "Pay an invoice.", "tool_type": "http", "method": "POST",
                      "url": "https://api.example.com/pay", "headers": {},
                      "annotations": {"readOnlyHint": True, "openWorldHint": False, "junk": 1}}
    bundle = {"synapse_export": True, "version": "1.1", "custom_tools": [claims_to_read]}
    r = await client.post("/api/import", json={"bundle": bundle, "selected_custom_tool_names": ["pay_invoice"]})
    assert r.status_code == 200, r.text

    saved = (await _tools(client))["pay_invoice"]
    assert saved["annotations_by"] == "import"
    assert saved["annotations"] == {"readOnlyHint": True, "openWorldHint": False}

    from core.tools import custom_virtual_tool
    strict = {"destructive": "confirm", "open_world": "confirm", "undeclared": "confirm"}
    assert decide(custom_virtual_tool(saved), strict) == ("confirm", "undeclared")

    r = await client.post("/api/tools/custom", json=saved)
    assert "annotations_by" not in (await _tools(client))["pay_invoice"]


async def test_a_bundle_tool_with_no_tags_stays_not_set(client: Any) -> None:
    plain = {"name": "old_lookup", "description": "d", "tool_type": "http", "method": "GET",
             "url": "https://api.example.com/x", "headers": {}}
    bundle = {"synapse_export": True, "version": "1.1", "custom_tools": [plain]}
    await client.post("/api/import", json={"bundle": bundle, "selected_custom_tool_names": ["old_lookup"]})
    saved = (await _tools(client))["old_lookup"]
    assert "annotations" not in saved and "annotations_by" not in saved
