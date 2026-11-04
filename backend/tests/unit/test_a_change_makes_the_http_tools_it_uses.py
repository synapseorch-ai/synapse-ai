"""A change makes the HTTP tools it uses, in code, in the change itself (F1322, CAP3's prod-13 P1 on 63dfb505; Naveen's
pick (A), eb 11-04).

prod-13's person asked a change to "add the necessary HTTP and email tools to the agents". The change planner sent
`create` for each tool with its URL and method: refused, since `create` was a hand-over taking only `what`, and one
among other ops is "its own request". It called `create_http_tool` four times, never one of its tools. Then it gave
the agents the four names nothing had made, and code passed that: the review offered "Change the tools of
orders_fetcher — tools: [fetch_uninvoiced_orders]", which nothing could do.

Now `create` of an HTTP tool with its url is the op `create_tool`: `create_http_tool`'s own facts as fields, held to
that tool's own checks before the review (`builder_tools.http_tool_issues`, the one the tool runs), shown with its
method and URL, made first after the yes through the registry, and undone by deleting it. Several ride one change with
the ops that use them. An agent is given only a tool that is here, or one this change makes. Where its key goes
(`key` {header, scheme}, never the key) puts the build's own Connect card up once it is made. An agent, a skill, a
Python tool, an OpenAPI import and a tool asked for in words only are still handed to the maker, as before.
"""
from __future__ import annotations

import json

import pytest

from core import builder_access
from core.builder_tools import execute_builder_tool_with_events
from core.models_orchestration import OrchestrationRun
from core.native_builder.change_tools import KEY_REFUSAL
from core.orchestration.engine import live
from core.orchestration.state import SharedState
from core.product_tools import confirm, hooks
from core.product_tools.roles import Role

SHOP = "https://shop.tidewell.example"
BILLING = "https://billing.ledgerline.example"


class _Server:
    agent_sessions: dict = {}
    tool_router: dict = {}


@pytest.fixture(autouse=True)
def fresh():
    confirm.set_claim_store(None)
    builder_access.forget_runs()

    async def owner(user_id):
        return Role.OWNER

    hooks.set_role_source(owner)
    yield
    hooks.set_role_source(None)
    builder_access.forget_runs()


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch):
    """Every named host is a public address: these tests are about the change, never the network's DNS (F1238)."""
    from core import egress_guard

    async def resolve(host):
        return ["93.184.216.34"]

    monkeypatch.setattr(egress_guard, "resolve", resolve)


_N = {"n": 0}


async def _run():
    _N["n"] += 1
    run = OrchestrationRun(run_id=f"builder_f1322_{_N['n']}", orchestration_id="orch_native_builder",
                           shared_state={builder_access.CALLER_KEY: {"user_id": "u1"},
                                         "user_message": "add the necessary HTTP tools to the agents"})
    await SharedState(run).checkpoint()
    return run


async def _call(tool, args, run):
    with live(run):
        raw, events = await execute_builder_tool_with_events(tool, args, _Server(), run_id=run.run_id)
    return json.loads(raw), events


async def _submitted(ops: list[dict]) -> list[dict]:
    """The planner's finish, as `submit_change` takes it: the ops it stores, or the test fails with what it sent back."""
    out, _ = await _call("submit_change", {"ops": ops}, await _run())
    assert out.get("finish") == "submit_change", out
    return out["ops"]


async def _change(draft: dict, answer: str = "approve"):
    """One whole change: check, review answered, approve, apply. Returns (run, check, apply, the apply's events)."""
    run = await _run()
    checked, _ = await _call("check_change", {"draft": {"finish": "submit_change", **draft}}, run)
    if checked["outcome"] != "review":
        return run, checked, None, []
    run.shared_state["change_checked"] = json.dumps(checked)
    run.shared_state["manage_review"] = {"feedback": answer}
    await _call("approve_change", {}, run)
    applied, events = await _call("apply_change", {}, run)
    return run, checked, applied, events


async def _agent(**kw):
    from core.store.resources import save_agent
    from tests._fakes.seed import make_agent

    agent = make_agent(**kw)
    await save_agent(agent)
    return agent


async def _tools() -> dict[str, dict]:
    from core.store.resources import load_tools

    return {t["name"]: t for t in await load_tools()}


async def _holds(agent_id: str) -> list[str]:
    from core.store.resources import get_agent

    return list((await get_agent(agent_id) or {}).get("tools") or [])


def _new_tool(name: str, url: str, method: str = "GET", **fields) -> dict:
    return {"op": "create", "target": {"kind": "tool", "name": name},
            "fields": {"description": f"{name.replace('_', ' ')}", "method": method, "url": url, **fields}}


def _give(agent_id: str, *names: str) -> dict:
    return {"op": "set_agent_tools", "target": {"kind": "agent", "id": agent_id}, "fields": {"add": list(names)}}


# ── prod-13's change ────────────────────────────────────────────────────────────────────────────────────────────────

async def test_prod_13s_change_makes_its_tools_and_gives_them_to_its_agents(client):
    fetcher = await _agent(name="orders_fetcher", tools=[])
    invoicer = await _agent(name="invoice_creator", tools=[])
    # As the planner wrote it: an agent given its tool before the tool is made.
    ops = await _submitted([
        _give(fetcher["id"], "fetch_uninvoiced_orders"),
        _new_tool("fetch_uninvoiced_orders", f"{SHOP}/orders?status=uninvoiced"),
        _new_tool("create_ledgerline_invoice", f"{BILLING}/invoices", "POST",
                  body='{"order_id": "{{ order_id }}", "total": {{ total }}}'),
        _give(invoicer["id"], "create_ledgerline_invoice"),
    ])
    assert [o["op"] for o in ops] == ["create_tool", "create_tool", "set_agent_tools", "set_agent_tools"], \
        "each tool is made before the op that gives it to an agent"

    _, checked, done, _ = await _change({"ops": ops})
    assert checked["outcome"] == "review", checked
    lines = checked["review"].splitlines()
    assert lines[0].startswith(f'- Make the tool "fetch_uninvoiced_orders" — an HTTP tool: GET '
                               f'{SHOP}/orders?status=uninvoiced;'), lines
    assert f"POST {BILLING}/invoices" in lines[1] and "order_id" in lines[1] and "total" in lines[1], lines

    assert done["applied"] == ["create_tool", "create_tool", "set_agent_tools", "set_agent_tools"], done
    tools = await _tools()
    assert tools["fetch_uninvoiced_orders"]["url"] == f"{SHOP}/orders?status=uninvoiced"
    assert tools["fetch_uninvoiced_orders"]["method"] == "GET"
    made = tools["create_ledgerline_invoice"]
    assert made["method"] == "POST" and made["body"] == '{"order_id": "{{ order_id }}", "total": {{ total }}}'
    assert {"order_id", "total"} <= set((made["inputSchema"] or {}).get("properties") or {}), \
        "what its URL and body read are its inputs"
    assert await _holds(fetcher["id"]) == ["fetch_uninvoiced_orders"]
    assert await _holds(invoicer["id"]) == ["create_ledgerline_invoice"]


async def test_its_undo_takes_the_tool_off_the_agent_and_deletes_it(client):
    fetcher = await _agent(name="orders_fetcher", tools=[])
    ops = await _submitted([_new_tool("fetch_uninvoiced_orders", f"{SHOP}/orders"),
                            _give(fetcher["id"], "fetch_uninvoiced_orders")])
    first, _, done, _ = await _change({"ops": ops})
    assert done["applied"] == ["create_tool", "set_agent_tools"], done
    _, checked, undone, _ = await _change({"undo_of": first.run_id})
    assert undone["applied"] == ["set_agent_tools", "delete_tool"], (checked, undone)
    assert "fetch_uninvoiced_orders" not in await _tools() and await _holds(fetcher["id"]) == []


# ── an agent is given only a tool that is here ─────────────────────────────────────────────────────────────────────

async def test_a_tool_nothing_has_is_refused_by_name_before_the_review(client):
    fetcher = await _agent(name="orders_fetcher", tools=[])
    _, checked, done, _ = await _change({"ops": [_give(fetcher["id"], "fetch_uninvoiced_orders")]})
    assert checked["outcome"] == "said" and done is None, checked
    assert 'No tool is called "fetch_uninvoiced_orders"' in checked["say"], checked["say"]


async def test_a_tool_that_is_here_is_given(client):
    from core.store.resources import save_tool

    await save_tool({"name": "lookup_order", "description": "Looks an order up", "tool_type": "http",
                     "method": "GET", "url": f"{SHOP}/orders/{{{{ id }}}}", "headers": {},
                     "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}}})
    fetcher = await _agent(name="orders_fetcher", tools=[])
    _, checked, done, _ = await _change({"ops": [_give(fetcher["id"], "lookup_order")]})
    assert checked["outcome"] == "review" and done["applied"] == ["set_agent_tools"], checked
    assert await _holds(fetcher["id"]) == ["lookup_order"]


# ── create_http_tool's own checks, before the review ─────────────────────────────────────────────────────────────

async def _refused(op: dict) -> str:
    _, checked, done, _ = await _change({"ops": [op]})
    assert checked["outcome"] == "said" and done is None, checked
    return checked["say"]


async def test_an_address_inside_our_network_is_refused_in_the_tools_own_words(client):
    from core.builder_tools import _refuse_inward_url

    url = "http://169.254.169.254/latest/meta-data"
    said = await _refused(_new_tool("read_metadata", url))
    assert (await _refuse_inward_url(url))["error"] in said, said
    assert "read_metadata" not in await _tools()


async def test_a_tool_that_places_calls_is_refused_in_the_tools_own_words(client):
    from core.builder_guard import PLACES_CALLS

    said = await _refused(_new_tool("ring_customer", "https://api.vapi.ai/call", "POST"))
    assert PLACES_CALLS in said, said


async def test_a_name_that_is_no_tool_name_is_refused_in_the_tools_own_words(client):
    said = await _refused(_new_tool("fetch orders!", f"{SHOP}/orders"))
    assert "name must be a tool-style identifier" in said, said


async def test_a_key_in_its_headers_is_never_kept(client):
    said = await _refused(_new_tool("fetch_orders", f"{SHOP}/orders",
                                    headers={"Authorization": "Bearer sk-live-abcdefghijklmnopqrstuvwx1234"}))
    assert said.startswith(KEY_REFUSAL) or KEY_REFUSAL in said, said
    assert "sk-live" not in said and "fetch_orders" not in await _tools()


async def test_an_input_its_url_reads_that_its_schema_leaves_out_is_refused(client):
    said = await _refused(_new_tool("get_order", f"{SHOP}/orders/{{{{ order_id }}}}",
                                    input_schema={"type": "object", "properties": {"id": {"type": "string"}}}))
    assert "order_id" in said, said


# ── where its key goes ─────────────────────────────────────────────────────────────────────────────────────────────

async def test_where_its_key_goes_is_on_the_review_and_its_card_comes_after_the_make(client):
    ops = await _submitted([_new_tool("fetch_orders", f"{SHOP}/orders", key={"header": "X-Api-Key"}),
                            _new_tool("mark_invoiced", f"{SHOP}/orders/{{{{ order_id }}}}", "PATCH",
                                      body='{"status": "invoiced"}', key={"header": "X-Api-Key"})])
    _, checked, done, events = await _change({"ops": ops})
    assert "X-Api-Key" in checked["review"] and "card" in checked["review"], checked["review"]
    assert done["applied"] == ["create_tool", "create_tool"], done
    [card] = [e for e in events if e.get("type") == "connection_needed"]
    assert card["kind"] == "tool_secret" and card["tool"]["header"] == "X-Api-Key"
    assert sorted(card["tool"]["tools"]) == ["fetch_orders", "mark_invoiced"], "one card for one host's tools"
    tools = await _tools()
    assert tools["fetch_orders"]["base_lock"] == SHOP, "a tool that sends a key keeps to its host"
    assert "card" in done["effect"], done


@pytest.mark.parametrize("key", [{"header": "X-Api-Key", "value": "abc123"}, {"scheme": "Bearer"}, "X-Api-Key"])
async def test_key_says_where_a_key_goes_never_the_key(client, key):
    out, _ = await _call("submit_change", {"ops": [_new_tool("fetch_orders", f"{SHOP}/orders", key=key)]},
                         await _run())
    assert "never the key" in out.get("error", ""), out
    assert "abc123" not in json.dumps(out)


async def test_a_tool_that_sends_a_key_keeps_its_host_fixed(client):
    said = await _refused(_new_tool("fetch_orders", "https://{{ region }}.shop.tidewell.example/orders",
                                    key={"header": "X-Api-Key"}))
    assert "host" in said, said


# ── what is still handed over ──────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("op", [
    {"op": "create", "target": {"kind": "tool", "name": "order_reader"}, "fields": {"what": "reads our orders"}},
    {"op": "create", "target": {"kind": "agent", "name": "Order clerk"}, "fields": {"what": "answers order questions"}},
])
async def test_asked_for_in_words_or_another_kind_it_is_still_handed_to_the_maker(client, op):
    _, checked, _, _ = await _change({"ops": [op]})
    assert checked["outcome"] == "create", checked


# ── what the planner is told ───────────────────────────────────────────────────────────────────────────────────────

def test_the_planner_is_told_an_http_tools_fields():
    from pathlib import Path

    from core.native_builder.change_tools import SUBMIT

    told = SUBMIT["function"]["description"]
    for field in ("url", "method", "headers", "body", "input_schema", "key"):
        assert field in told.split("create (an HTTP tool", 1)[-1], field
    rules = json.loads((Path(__file__).resolve().parents[2] / "core" / "native_builder" / "agents" /
                        "planner.json").read_text())["system_prompt"]
    changing = rules.split("\n# Changing", 1)[1].split("\n# ", 1)[0]  # rule 5 of its own section
    rule = next(line for line in changing.splitlines() if line.startswith("5. "))
    assert "url" in rule and "what" in rule, rule


# ── the lock: code's own, from the URL the person approved (eb 11-04, option (1)) ─────────────────────────────────

async def _invoked_as_a_client(args: dict) -> dict:
    """`create_http_tool` called as an MCP client calls it: its confirm token redeemed when it asks for one."""
    from core.product_tools import areas, build, confirm, registry
    from core.product_tools.caller import Caller

    areas.ensure_loaded()
    caller = Caller(role=Role.OWNER, via="mcp", user_id="u1")
    with build.using_module(_Server()):
        out = await registry.invoke("create_http_tool", caller, dict(args))
        if isinstance(out, dict) and out.get("status") == "needs_confirmation":
            out = await registry.invoke("create_http_tool", caller, {**args, confirm.TOKEN_ARG: out["confirm_token"]})
    return json.loads(out) if isinstance(out, str) else out


@pytest.mark.parametrize("path", ["mcp_client", "maker"])
async def test_a_callers_lock_is_never_stored(client, path):
    args = {"name": "someones_tool", "description": "d", "url": f"{SHOP}/orders", "method": "GET",
            "base_lock": "https://elsewhere.example"}
    if path == "mcp_client":
        await _invoked_as_a_client(args)
    else:
        out, _ = await _call("create_http_tool", args, await _run())
        assert out.get("status") == "created", out
    assert "base_lock" not in (await _tools())["someones_tool"], "only Synapse's own save sets a lock"


async def test_the_changes_lock_is_the_approved_urls_origin_and_never_wider(client):
    from core.native_builder import change_ops as C

    ops = await _submitted([_new_tool("get_order", f"{SHOP}/v2/orders/{{{{ order_id }}}}",
                                      key={"header": "Authorization", "scheme": "Bearer"})])
    _, checked, done, _ = await _change({"ops": ops})
    assert done["applied"] == ["create_tool"], (checked, done)
    assert (await _tools())["get_order"]["base_lock"] == SHOP, "its origin: the host the person approved"
    assert C.http_tool_args("x", {"url": f"{SHOP}/a/b", "key": {"header": "X"}})["base_lock"] == SHOP
    # A change names no lock of its own, and never re-makes a tool that has one.
    out, _ = await _call("submit_change", {"ops": [_new_tool("other", f"{SHOP}/x", key={"header": "X-Api-Key"},
                                                             base_lock="https://")]}, await _run())
    assert "cannot set base_lock" in out.get("error", ""), out
    _, again, _, _ = await _change({"ops": await _submitted([
        _new_tool("get_order", "https://elsewhere.example/orders", key={"header": "X-Api-Key"})])})
    assert again["outcome"] == "said" and "already called" in again["say"], again
    assert (await _tools())["get_order"]["base_lock"] == SHOP
