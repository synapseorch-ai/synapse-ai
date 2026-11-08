"""The AI builder can say a tool it makes is riskier than its method, never safer (TAG1, D296, F735).

BWA1's decision 4 left the builder's custom tools with their method's hints only: a GET that cancels a booking, or a
POST that sends an invoice, could not be marked as such. `create_http_tool` and `import_openapi` now take the four
tags; each claim passes `core.tool_tags.tighten`, so the builder can mark a tool destructive or not safe to repeat but
cannot call a POST read-only. Its tags stay marked as its own (a claim). Replacing or re-importing a tool the person
tagged starts from the person's tags. The guard still lets a builder-made GET read without a Human step.
"""
from __future__ import annotations

import json

from core.builder_tools import execute_builder_tool
from core.tool_policy import decide
from core.tool_tags import defaults_for, hints_of, tags_of
from core.tools import BUILDER_HINTS, custom_virtual_tool, method_hints


class _Server:
    agent_sessions: dict = {}


async def _call(name: str, args: dict) -> dict:
    return json.loads(await execute_builder_tool(name, args, _Server()))


async def _saved(name: str) -> dict:
    from core.routes.tools import load_custom_tools
    (tool,) = [t for t in await load_custom_tools() if t["name"] == name]
    return tool


async def test_it_cannot_call_a_post_read_only():
    got = await _call("create_http_tool", {"name": "send_invoice", "description": "Send an invoice.", "method": "POST",
                                           "url": "https://api.example.com/invoices", "read_only": True,
                                           "idempotent": True, "open_world": False})
    assert got["status"] == "created", got
    tool = await _saved("send_invoice")
    assert tool["annotations"] == method_hints("POST") and tool["annotations_by"] == BUILDER_HINTS


async def test_it_can_mark_a_get_that_cancels_destructive():
    await _call("create_http_tool", {"name": "cancel_via_get", "description": "Cancel a booking.", "method": "GET",
                                     "url": "https://api.example.com/cancel?id={{ id }}", "destructive": True,
                                     "idempotent": False})
    tool = await _saved("cancel_via_get")
    assert tags_of(tool["annotations"]) == {"read_only": False, "destructive": True, "idempotent": False,
                                            "open_world": True}
    strict = {"destructive": "confirm", "open_world": "allow", "undeclared": "allow"}
    assert decide(custom_virtual_tool(tool), strict) == ("confirm", "destructive")


async def test_replacing_a_tool_the_person_tagged_starts_from_their_tags():
    from core.routes.tools import _upsert_custom_tools

    mine = {**method_hints("POST"), "idempotentHint": True, "openWorldHint": False}
    await _upsert_custom_tools([{"name": "upsert_contact", "description": "d", "method": "POST",
                                 "url": "https://crm.example.com/contacts", "annotations": mine}])
    await _call("create_http_tool", {"name": "upsert_contact", "description": "Upsert a contact.", "method": "POST",
                                     "url": "https://crm.example.com/v2/contacts"})
    tool = await _saved("upsert_contact")
    assert tool["annotations"] == hints_of(tags_of(mine)) and "annotations_by" not in tool

    await _call("create_http_tool", {"name": "upsert_contact", "description": "Upsert a contact.", "method": "POST",
                                     "url": "https://crm.example.com/v2/contacts", "destructive": True})
    assert tags_of((await _saved("upsert_contact"))["annotations"])["destructive"] is True


SPEC = {
    "openapi": "3.0.0", "info": {"title": "Billing"}, "servers": [{"url": "https://billing.example.com"}],
    "paths": {"/invoices": {"get": {"operationId": "list_invoices"}, "post": {"operationId": "send_invoice_now"}}},
}


async def test_an_import_takes_its_tags_per_operation_and_only_stricter():
    got = await _call("import_openapi", {"spec_text": json.dumps(SPEC), "tags_json": json.dumps({
        "send_invoice_now": {"destructive": True}, "list_invoices": {"read_only": False, "open_world": False}})})
    assert got["status"] == "imported", got
    sent, listed = await _saved("send_invoice_now"), await _saved("list_invoices")
    assert tags_of(sent["annotations"])["destructive"] is True and sent["annotations_by"] == BUILDER_HINTS
    assert tags_of(listed["annotations"]) == {**defaults_for("GET"), "read_only": False}


async def test_an_import_keeps_the_persons_tags():
    from core.routes.tools import _upsert_custom_tools

    await _call("import_openapi", {"spec_text": json.dumps(SPEC)})
    tool = await _saved("send_invoice_now")
    mine = {**method_hints("POST"), "openWorldHint": False}
    await _upsert_custom_tools([{**tool, "annotations": mine}])
    await _call("import_openapi", {"spec_text": json.dumps(SPEC)})
    again = await _saved("send_invoice_now")
    assert tags_of(again["annotations"]) == tags_of(mine) and "annotations_by" not in again


async def test_its_tool_list_says_what_each_tool_does():
    await _call("create_http_tool", {"name": "find_room", "description": "Find a room.", "method": "GET",
                                     "url": "https://b.example.com/rooms"})
    from core.routes.tools import _upsert_custom_tools
    await _upsert_custom_tools([{"name": "old_one", "description": "d", "tool_type": "python", "code": "x"}])
    listed = {t["name"]: t for t in await _call("list_custom_tools", {})}
    assert listed["find_room"]["tags"] == defaults_for("GET") and listed["find_room"]["tags_by"] == "builder"
    assert listed["old_one"]["tags"] == "not set"


async def test_the_guard_still_lets_a_builder_made_get_read():
    from core.builder_guard import unapproved

    await _call("create_http_tool", {"name": "search_rooms2", "description": "Search.", "method": "GET",
                                     "url": "https://b.example.com/s"})
    reads = {"id": "o", "entry_step_id": "s1", "steps": [
        {"id": "s1", "type": "tool", "forced_tool": "search_rooms2", "next_step_id": "end"}, {"id": "end", "type": "end"}]}
    assert (await unapproved([reads], {}, _Server(), made_agents=set(), exempt=set()))[0] == []
