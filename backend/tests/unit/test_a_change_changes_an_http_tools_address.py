"""A change changes an HTTP tool's address, method or what it does, and only those (F1312b, F1312's split; F1310's
B1; Naveen 11-04, eb).

prod-07's workflow called a tool at an address that was wrong, and no change could fix it: F1310's card could only say
where to fix it by hand. Today's ways to save a tool replace it whole by name, so a change would have carried the
tool's headers — its key among them — into the review it shows and keeps. Now `update_http_tool` edits a tool's url,
method and description in place and never takes or returns its headers; the op `update_tool` proposes it, held
before the review to create_http_tool's own checks (F1322/F1309's http_tool_issues), the tool's lock (F944), no user
part and no key in the address. A variable the new address reads is added to the tool's inputs, none taken away, and
the review says so; its line shows the address before and after and what uses the tool. Its undo puts the address
back and takes out only the inputs it added. Its facts may come from a curl the person pasted (F1312a's `from`), and
the planner asks for the address in words, its form offered with today's address filled in.

Every key below is assembled at run time: a literal one in a source file trips secret scanners.
"""
from __future__ import annotations

import json

import pytest

from core.native_builder.change_tools import KEY_REFUSAL
from test_a_change_makes_the_http_tools_it_uses import (  # noqa: F401 — fixtures
    SHOP, _agent, _call, _change, _holds, _public_dns, _tools, fresh)

KEY = "shop-" + "saved-key-" + "8812"
CARD_KEY = "sk-live-" + "abcdefghijklmnopqrstuvwx" + "1234"


@pytest.fixture(autouse=True)
def _read(monkeypatch):
    """No site is fetched here (F1312a's pack reads the sites a message names)."""
    from core.native_builder import named_sites

    async def read_named(text, already=None):
        return {}

    monkeypatch.setattr(named_sites, "read_named", read_named)


async def _tool(name="fetch_orders", url=f"{SHOP}/orders?status=new", method="GET", **more) -> dict:
    from core.store.resources import save_tool

    tool = {"name": name, "description": "Fetches the shop's orders", "tool_type": "http", "method": method,
            "url": url, "headers": {"X-Api-Key": KEY},
            "inputSchema": {"type": "object", "properties": {"status": {"type": "string"}}}, **more}
    await save_tool(tool)  # as its Connect card left it: its key in its header
    return tool


def _update(name="fetch_orders", **fields) -> dict:
    return {"op": "update_tool", "target": {"kind": "tool", "name": name}, "fields": fields}


async def _submitted(ops: list[dict], message: str = "fix the tool's address") -> dict:
    from core import builder_access
    from core.models_orchestration import OrchestrationRun
    from core.native_builder.door import check
    from core.orchestration.state import SharedState

    run = OrchestrationRun(run_id=f"builder_f1312b_{abs(hash(message))}", orchestration_id="orch_native_builder",
                           shared_state={builder_access.CALLER_KEY: {"user_id": "u1"},
                                         "user_message": check(message).text})
    await SharedState(run).checkpoint()
    out, _ = await _call("submit_change", {"ops": ops}, run)
    return out


# ── the address, changed in place ───────────────────────────────────────────────────────────────────────────────────

async def test_its_address_is_changed_and_its_key_stays_where_it_is(client):
    await _tool()
    fetcher = await _agent(name="orders_fetcher", tools=["fetch_orders"])
    new = f"{SHOP}/v2/orders?status=uninvoiced"
    _, checked, done, _ = await _change({"ops": [_update(url=new)]})
    assert checked["outcome"] == "review", checked
    [line] = checked["review"].splitlines()
    assert line.startswith(f'- Change the tool "fetch_orders" — an HTTP tool: GET {SHOP}/orders?status=new → GET '
                           f'{new}'), line
    assert '"orders_fetcher"' in line, "what uses it is on its line"
    assert KEY not in json.dumps(checked), "its key is never in the review, the card or the call"
    assert done["applied"] == ["update_tool"], done
    tool = (await _tools())["fetch_orders"]
    assert tool["url"] == new and tool["headers"] == {"X-Api-Key": KEY}, "its key stays where its card put it"
    assert await _holds(fetcher["id"]) == ["fetch_orders"]


async def test_its_method_and_what_it_does_are_changed_too(client):
    await _tool()
    _, _, done, _ = await _change({"ops": [_update(method="post", description="Creates an order")]})
    assert done["applied"] == ["update_tool"], done
    tool = (await _tools())["fetch_orders"]
    assert tool["method"] == "POST" and tool["description"] == "Creates an order" and tool["url"].endswith("status=new")


async def test_a_new_variable_in_its_address_is_a_new_input_and_none_is_taken_away(client):
    await _tool()
    new = f"{SHOP}/orders/{{{{ order_id }}}}?since={{{{ since }}}}"
    _, checked, done, _ = await _change({"ops": [_update(url=new)]})
    assert "now asks for order_id, since" in checked["review"], checked["review"]
    assert done["applied"] == ["update_tool"], done
    schema = (await _tools())["fetch_orders"]["inputSchema"]
    assert set(schema["properties"]) == {"status", "order_id", "since"}, schema
    assert "order_id" in schema.get("required", []) and "since" not in schema.get("required", []), \
        "a path's variable it needs; a query's it may leave out"


async def test_its_undo_puts_its_address_back_and_takes_out_only_what_it_added(client):
    await _tool()
    first, _, done, _ = await _change({"ops": [_update(url=f"{SHOP}/orders/{{{{ order_id }}}}")]})
    assert done["applied"] == ["update_tool"], done
    _, checked, undone, _ = await _change({"undo_of": first.run_id})
    assert undone and undone["applied"] == ["update_tool"], (checked, undone)
    tool = (await _tools())["fetch_orders"]
    assert tool["url"] == f"{SHOP}/orders?status=new" and set(tool["inputSchema"]["properties"]) == {"status"}
    assert tool["headers"] == {"X-Api-Key": KEY}


# ── what it is held to, before the review ───────────────────────────────────────────────────────────────────────────

async def _refused(op: dict) -> str:
    _, checked, done, _ = await _change({"ops": [op]})
    assert checked["outcome"] == "said" and done is None, checked
    return checked["say"]


async def test_a_locked_tool_moves_under_its_lock_and_never_off_it(client):
    await _tool(base_lock=SHOP)
    said = await _refused(_update(url="https://elsewhere.example/orders"))
    assert "keeps to that API" in said, said
    _, _, done, _ = await _change({"ops": [_update(url=f"{SHOP}/v2/orders")]})
    assert done["applied"] == ["update_tool"] and (await _tools())["fetch_orders"]["base_lock"] == SHOP


async def test_an_address_inside_our_network_is_refused_in_the_tools_own_words(client):
    from core.builder_tools import _refuse_inward_url

    await _tool()
    url = "http://169.254.169.254/latest/meta-data"
    assert (await _refuse_inward_url(url))["error"] in await _refused(_update(url=url))


async def test_an_address_that_places_calls_is_refused(client):
    from core.builder_guard import PLACES_CALLS

    await _tool()
    assert PLACES_CALLS in await _refused(_update(url="https://api.vapi.ai/call", method="POST"))


async def test_an_address_with_a_user_part_is_refused(client):
    await _tool()
    said = await _refused(_update(url=f"https://alice:{'p' + 'w'}@shop.tidewell.example/orders"))
    assert "user" in said and "pw@" not in said, said


async def test_a_key_in_its_address_is_never_kept(client):
    await _tool()
    said = await _refused(_update(url=f"{SHOP}/orders?api_key={CARD_KEY}"))
    assert KEY_REFUSAL in said and CARD_KEY not in said, said
    assert (await _tools())["fetch_orders"]["url"] == f"{SHOP}/orders?status=new"


async def test_a_tool_that_is_not_an_http_tool_is_refused(client):
    from core.store.resources import save_tool

    await save_tool({"name": "region_totals", "description": "Totals", "tool_type": "python", "code": "print(1)"})
    said = await _refused(_update(name="region_totals", url=f"{SHOP}/x"))
    assert "HTTP tool" in said, said


# ── from what the person pasted ─────────────────────────────────────────────────────────────────────────────────────

async def test_its_address_can_come_from_a_curl_and_the_curls_key_changes_nothing(client):
    await _tool()
    curl = f"curl -X POST {SHOP}/v2/orders -H 'X-Api-Key: {CARD_KEY}' -d '{{\"status\": \"new\"}}'"
    out = await _submitted([_update(**{"from": {"curl": 1}})], message=f"Its new call is: {curl}")
    assert out.get("finish") == "submit_change", out
    [op] = out["ops"]
    assert op["fields"]["url"] == f"{SHOP}/v2/orders" and op["fields"]["method"] == "POST", op
    assert set(op["fields"]) <= {"url", "method", "description", "from"}, "an update never carries headers or a key"
    _, checked, done, _ = await _change({"ops": out["ops"]})
    assert "its key stays on its card" in checked["review"], checked["review"]
    assert done["applied"] == ["update_tool"] and (await _tools())["fetch_orders"]["headers"] == {"X-Api-Key": KEY}


# ── the registry tool, and what the planner is told ─────────────────────────────────────────────────────────────────

def test_update_http_tool_takes_no_headers_and_no_agent_reaches_it_through_all():
    from core.builder_tools import BUILDER_TOOL_SCHEMAS, OPT_IN_BUILDER_TOOLS

    [schema] = [t["function"]["parameters"] for t in BUILDER_TOOL_SCHEMAS
                if t["function"]["name"] == "update_http_tool"]
    assert not {k for k in schema["properties"] if "header" in k}, schema
    assert set(schema["required"]) == {"name"} and "update_http_tool" in OPT_IN_BUILDER_TOOLS


def test_the_planner_is_told_how_a_tools_address_is_fixed():
    from pathlib import Path

    from core.native_builder.change_tools import SUBMIT

    assert "update_tool {url, method, description" in SUBMIT["function"]["description"]
    rules = json.loads((Path(__file__).resolve().parents[2] / "core" / "native_builder" / "agents" /
                        "planner.json").read_text())["system_prompt"]
    changing = rules.split("\n# Changing", 1)[1].split("\n# ", 1)[0]
    rule = next(line for line in changing.splitlines() if "update_tool" in line)
    for word in ("address", "form", "today"):
        assert word in rule, (word, rule)


# ── Naveen's rulings on the shots (eb 11-04) ────────────────────────────────────────────────────────────────────────

CURL_HINT = "a curl is in the context: name it with from {curl: n}"
ADDRESS_HINT = "You can also paste a curl, its OpenAPI spec, or a link to its docs."


@pytest.mark.parametrize("op", [
    _update(url=f"{SHOP}/v2/orders"),
    {"op": "create", "target": {"kind": "tool", "name": "fetch_v2_orders"},
     "fields": {"description": "Fetches orders", "url": f"{SHOP}/v2/orders"}},
])
async def test_a_pasted_curl_is_named_with_from_and_asked_for_once_only(client, op):
    from core import builder_access
    from core.models_orchestration import OrchestrationRun
    from core.native_builder.door import check
    from core.orchestration.state import SharedState

    await _tool()
    run = OrchestrationRun(run_id=f"builder_f1312b_nudge_{op['op']}", orchestration_id="orch_native_builder",
                           shared_state={builder_access.CALLER_KEY: {"user_id": "u1"},
                                         "user_message": check(f"Its call is: curl {SHOP}/v2/orders").text})
    await SharedState(run).checkpoint()
    first, _ = await _call("submit_change", {"ops": [op]}, run)
    assert CURL_HINT in first.get("error", ""), first
    again, _ = await _call("submit_change", {"ops": [op]}, run)
    assert again.get("finish") == "submit_change", "said once: never a loop"


async def test_with_no_curl_in_the_context_nothing_is_asked(client):
    await _tool()
    out = await _submitted([_update(url=f"{SHOP}/v2/orders")], message="its address is now /v2/orders")
    assert out.get("finish") == "submit_change", out


@pytest.mark.parametrize("args", [
    {"question": "What is the correct address for the fetch_orders tool?", "about": "what",
     "subject": "address of fetch_orders"},
    {"question": "A few things before I plan it:", "questions": [
        {"question": "What is the correct URL for the fetch_orders tool?", "about": "what",
         "subject": "url of fetch_orders", "guess": f"{SHOP}/orders?status=new"}]},
])
async def test_the_address_question_says_what_else_helps_in_codes_words(client, args):
    from test_a_change_makes_the_http_tools_it_uses import _run

    out, _ = await _call("ask_person", args, await _run())
    assert out.get("finish") == "ask_person", out
    assert out["question"].rstrip().endswith(ADDRESS_HINT), out["question"]


async def test_a_question_about_anything_else_has_no_such_line(client):
    from test_a_change_makes_the_http_tools_it_uses import _run

    out, _ = await _call("ask_person", {"question": "What is the finance team's email address?", "about": "who",
                                        "subject": "finance email"}, await _run())
    assert ADDRESS_HINT not in out.get("question", ""), out


async def test_update_http_tool_is_served_to_mcp_clients_destructive_and_never_with_headers(client):
    from core.mcp_server import catalog
    from core.product_tools import areas, build, confirm, registry
    from core.product_tools.caller import Caller
    from core.product_tools.roles import Role

    [served] = [t for t in catalog.builder_tools() if t.name == "update_http_tool"]
    assert served.annotations.destructiveHint and not served.annotations.readOnlyHint
    assert not {k for k in (served.inputSchema.get("properties") or {}) if "header" in k}, served.inputSchema
    await _tool()
    areas.ensure_loaded()
    caller, args = Caller(role=Role.OWNER, via="mcp", user_id="u1"), {"name": "fetch_orders", "url": f"{SHOP}/v2"}
    with build.using_module(None):
        out = await registry.invoke("update_http_tool", caller, args)
        if isinstance(out, dict) and out.get("status") == "needs_confirmation":
            assert KEY not in json.dumps(out), "its preview never shows the key"
            out = await registry.invoke("update_http_tool", caller, {**args, confirm.TOKEN_ARG: out["confirm_token"]})
    assert KEY not in json.dumps(out, default=str) and "headers" not in json.dumps(out, default=str), out
    assert (await _tools())["fetch_orders"]["url"] == f"{SHOP}/v2"
