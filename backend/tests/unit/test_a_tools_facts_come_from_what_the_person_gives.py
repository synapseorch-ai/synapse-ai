"""An HTTP tool's facts come from whatever the person gives: a URL, a pasted curl, an OpenAPI spec, a docs link or
plain words (F1312a, the shared seam of F1312's split; Naveen 11-04, eb).

F1322 made a change make the HTTP tools it uses from fields the planner writes. A person rarely has those fields to
hand: they paste a curl from the API's docs, link the spec, or say it in words, and they answer in the chat as often
as in a form. Now:

* the door masks a curl's key-carrying parts by their name, at any length, before the model or the run's state sees
  them (Naveen's list: Authorization, Proxy-Authorization, Cookie, any header whose name holds key/token/secret/auth,
  -u, --oauth2-bearer, key-named query parameters); every other part, and every other text, is as it was;
* code turns a curl into the tool's facts (`core.http_facts`): its method, URL, query, the headers that carry no
  key, its body, and `key {header, scheme}` where its key went; a key in its query becomes a workspace variable;
  a local file or a second URL is refused in words;
* the planner's pack carries every curl in the message and in the latest reply, as facts, numbered; `create_tool`
  names one, or a spec operation the conversation read, with `from`, and code fills the fields from it; the planner
  marks what varies with {{ }}, held to the curl's shape, so the call never moves;
* the ask is chat first: a reply typed instead of a form answers it, and a curl or a link in that reply is read by
  code on that turn.

Every key below is assembled at run time: a literal one in a source file trips secret scanners.
"""
from __future__ import annotations

import json

import pytest

from core.native_builder.door import CREDENTIAL_MARK, check
from core.secret_scrub import holds_credential, scrub_credentials
from test_a_change_makes_the_http_tools_it_uses import (  # noqa: F401 — fixtures
    _call, _change, _public_dns, _tools, fresh)

SHORT = "zq" + "8L" + "k2"  # six characters: no shape the door knows, a key all the same
BILLING = "https://billing.ledgerline.example"


@pytest.fixture(autouse=True)
def _read(monkeypatch):
    """No site is fetched here: what the pack asks `named_sites` to read is recorded instead."""
    from core.native_builder import named_sites

    asked: list[str] = []

    async def read_named(text, already=None):
        asked.append(text)
        return {}

    monkeypatch.setattr(named_sites, "read_named", read_named)
    return asked


def _masked(text: str) -> str:
    return scrub_credentials(text)[0]


# ── the door, for a pasted curl ─────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("part", [
    f"-H 'Authorization: Bearer {SHORT}'",
    f'-H "Proxy-Authorization: Basic {SHORT}"',
    f"-H 'Cookie: sid={SHORT}'",
    f"-H 'X-Api-Key: {SHORT}'",
    f"--header 'X-Auth: {SHORT}'",
    f"-u alice:{SHORT}",
    f"--oauth2-bearer {SHORT}",
    f"-b 'sid={SHORT}'",
])
def test_a_curls_key_carrying_part_is_masked_by_its_name(part):
    text = f"Here it is: curl {part} {BILLING}/invoices"
    kept = _masked(text)
    assert SHORT not in kept and CREDENTIAL_MARK in kept, kept
    assert holds_credential(text)
    assert check(text).text == kept, "the door's own path masks it"


def test_a_key_in_a_curls_query_is_masked_by_its_name():
    kept = _masked(f"curl '{BILLING}/invoices?api_key={SHORT}&status=open'")
    assert SHORT not in kept and "status=open" in kept, kept


def test_a_curls_other_parts_are_as_they_were():
    text = (f"curl -X POST {BILLING}/invoices -H 'Content-Type: application/json' --user-agent 'Shop/1.0' "
            "-H 'Authorization: Bearer {{ global.LEDGERLINE }}' -d '{\"order_id\": \"1234\"}'")
    assert _masked(text) == text


def test_prose_with_no_curl_is_as_it_was():
    text = f"Our lookup is at {BILLING}/items?key={SHORT} and the docs say -u is for users."
    assert _masked(text) == text


# ── code turns a curl into the tool's facts ─────────────────────────────────────────────────────────────────────────

def _facts(text: str) -> dict:
    from core import http_facts

    return http_facts.from_curl(_masked(text))


def test_a_curl_is_its_tools_facts_its_key_never_among_them():
    facts = _facts(f"curl -X POST '{BILLING}/invoices?draft=1' -H 'Authorization: Bearer {SHORT}' "
                   "-H 'Content-Type: application/json' -d '{\"order_id\": \"1234\", \"total\": 12.5}'")
    assert facts["method"] == "POST" and facts["url"] == f"{BILLING}/invoices?draft=1", facts
    assert facts["headers"] == {"Content-Type": "application/json"}, "a key's header never goes into the tool"
    assert facts["body"] == '{"order_id": "1234", "total": 12.5}'
    assert facts["key"] == {"header": "Authorization", "scheme": "Bearer"}
    assert SHORT not in json.dumps(facts) and CREDENTIAL_MARK not in json.dumps(facts)


@pytest.mark.parametrize("text", [
    f"curl {BILLING}/invoices \\\n  -H 'Accept: application/json'",
    f"curl {BILLING}/invoices ^\r\n  -H \"Accept: application/json\"",
])
def test_its_lines_join_as_a_shell_joins_them(text):
    facts = _facts(text)
    assert facts["url"] == f"{BILLING}/invoices" and facts["headers"] == {"Accept": "application/json"}, facts


@pytest.mark.parametrize("text, method, url, body", [
    (f"curl {BILLING}/invoices", "GET", f"{BILLING}/invoices", None),
    (f"curl {BILLING}/invoices -d 'a=1'", "POST", f"{BILLING}/invoices", "a=1"),
    (f"curl -G {BILLING}/invoices -d 'status=open'", "GET", f"{BILLING}/invoices?status=open", None),
    (f"curl --json '{{\"a\": 1}}' {BILLING}/invoices", "POST", f"{BILLING}/invoices", '{"a": 1}'),
    (f"curl -XDELETE {BILLING}/invoices/7", "DELETE", f"{BILLING}/invoices/7", None),
])
def test_its_method_is_the_one_a_shell_would_send(text, method, url, body):
    facts = _facts(text)
    assert (facts["method"], facts["url"], facts.get("body")) == (method, url, body), facts


def test_json_says_its_content_type():
    assert _facts(f"curl --json '{{}}' {BILLING}/x")["headers"].get("Content-Type") == "application/json"


def test_a_user_and_password_is_a_key_for_its_card():
    facts = _facts(f"curl -u alice:{SHORT} {BILLING}/invoices")
    assert facts["key"] == {"header": "Authorization", "scheme": ""} and SHORT not in json.dumps(facts)


def test_a_key_in_its_query_is_a_workspace_variable():
    facts = _facts(f"curl '{BILLING}/invoices?api_key={SHORT}&status=open'")
    [name] = facts["variables"]
    assert facts["url"] == f"{BILLING}/invoices?api_key={{{{ global.{name} }}}}&status=open", facts
    assert SHORT not in json.dumps(facts)


@pytest.mark.parametrize("text, words", [
    (f"curl -d @order.json {BILLING}/invoices", "file"),
    (f"curl -F 'file=@receipt.pdf' {BILLING}/upload", "file"),
    (f"curl {BILLING}/a {BILLING}/b", "one URL"),
    ("curl ftp://files.example/x", "http"),
    (f"curl -H 'Accept: x {BILLING}/a", "quote"),
])
def test_what_it_cannot_take_is_said(text, words):
    facts = _facts(text)
    assert words in facts.get("problem", ""), facts


def test_the_curls_in_a_message_are_found_at_most_three():
    from core import http_facts

    text = ("Here are the calls:\n```\ncurl https://a.example/x\n```\nand `curl -X POST https://b.example/y` and "
            "curl https://c.example/z and curl https://d.example/w")
    assert [http_facts.from_curl(c)["url"] for c in http_facts.curls_in(text)] == [
        "https://a.example/x", "https://b.example/y", "https://c.example/z"]


# ── the planner's {{ }} keep the curl's shape ───────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("template", [f"{BILLING}/orders/{{{{ order_id }}}}", f"{BILLING}/orders/1234",
                                      f"{BILLING}/orders/{{{{ order_id }}}}?status={{{{ status }}}}"])
def test_a_template_of_the_curl_keeps_its_shape(template):
    from core import http_facts

    literal = f"{BILLING}/orders/1234" + ("?status=open" if "status" in template else "")
    assert http_facts.same_shape(template, literal) is None


@pytest.mark.parametrize("template", ["https://elsewhere.example/orders/{{ order_id }}",
                                      f"{BILLING}/admin/{{{{ order_id }}}}", f"{BILLING}/orders/1234/extra",
                                      f"{BILLING}:8443/orders/1234"])
def test_a_template_that_moves_the_call_is_refused(template):
    from core import http_facts

    assert http_facts.same_shape(template, f"{BILLING}/orders/1234"), template


# ── the pack, and create_tool from a curl or a spec ─────────────────────────────────────────────────────────────────

CURL = (f"curl -X POST {BILLING}/invoices -H 'Authorization: Bearer {SHORT}' -H 'Content-Type: application/json' "
        "-d '{\"order_id\": \"1234\", \"total\": 12.5}'")


async def _run(message: str, **state):
    from core import builder_access
    from core.models_orchestration import OrchestrationRun
    from core.orchestration.state import SharedState

    run = OrchestrationRun(run_id=f"builder_f1312a_{abs(hash((message, json.dumps(state, default=str))))}",
                           orchestration_id="orch_native_builder",
                           shared_state={builder_access.CALLER_KEY: {"user_id": "u1"},
                                         "user_message": check(message).text, **state})
    await SharedState(run).checkpoint()
    return run


async def _pack(run) -> dict:
    out, _ = await _call("read_plan_context", {}, run)
    return out


async def test_the_pack_holds_each_curl_the_message_pastes_as_facts(client):
    pack = await _pack(await _run(f"Make a tool for this: {CURL}"))
    [curl] = pack["curls"]
    assert curl["n"] == 1 and curl["method"] == "POST" and curl["url"] == f"{BILLING}/invoices", curl
    assert curl["key"] == {"header": "Authorization", "scheme": "Bearer"} and SHORT not in json.dumps(pack)


async def test_a_curl_typed_as_the_reply_is_read_that_turn(client):
    run = await _run("Add a tool that makes the invoice", human_response={"answer": check(CURL).text})
    assert [c["url"] for c in (await _pack(run))["curls"]] == [f"{BILLING}/invoices"]


async def test_a_link_typed_as_the_reply_is_read_that_turn(client, _read):
    await _pack(await _run("Add a tool that makes the invoice",
                           human_response={"answer": "Its docs are at https://docs.ledgerline.example/api"}))
    assert any("https://docs.ledgerline.example/api" in t for t in _read), _read


async def _submitted_in(run, ops: list[dict]) -> dict:
    out, _ = await _call("submit_change", {"ops": ops}, run)
    return out


def _from_curl(name: str, **fields) -> dict:
    return {"op": "create", "target": {"kind": "tool", "name": name},
            "fields": {"from": {"curl": 1}, "description": "Makes an invoice in Ledgerline", **fields}}


async def test_a_new_tool_from_the_curl_is_filled_by_code_and_made_without_its_key(client):
    run = await _run(f"Add a tool that makes the invoice: {CURL}")
    body = '{"order_id": "{{ order_id }}", "total": {{ total }}}'
    out = await _submitted_in(run, [_from_curl("create_ledgerline_invoice", body=body)])
    assert out.get("finish") == "submit_change", out
    [op] = out["ops"]
    assert op["op"] == "create_tool" and op["fields"]["method"] == "POST" and \
        op["fields"]["url"] == f"{BILLING}/invoices", op
    assert op["fields"]["key"] == {"header": "Authorization", "scheme": "Bearer"}
    assert op["fields"]["headers"] == {"Content-Type": "application/json"} and op["fields"]["body"] == body
    _, checked, done, events = await _change({"ops": out["ops"]})
    assert f"POST {BILLING}/invoices" in checked["review"] and "Authorization" in checked["review"], checked
    assert done["applied"] == ["create_tool"], done
    made = (await _tools())["create_ledgerline_invoice"]
    assert made["headers"] == {"Content-Type": "application/json"}, "its key comes on its card, never from the chat"
    assert [e for e in events if e.get("type") == "connection_needed"], events


async def test_a_template_off_the_curls_shape_is_sent_back(client):
    run = await _run(f"Add a tool that makes the invoice: {CURL}")
    out = await _submitted_in(run, [_from_curl("x_invoice", url="https://elsewhere.example/invoices")])
    assert f"{BILLING}/invoices" in out.get("error", ""), out


async def test_a_curl_the_message_does_not_hold_is_sent_back(client):
    run = await _run(f"Add a tool that makes the invoice: {CURL}")
    out = await _submitted_in(run, [_from_curl("x_invoice") | {"fields": {"from": {"curl": 3},
                                                                           "description": "x"}}])
    assert "curl" in out.get("error", "") and "1" in out["error"], out


SPEC_TOOL = {"name": "ledgerline_create_invoice", "description": "Create an invoice", "method": "POST",
             "url": f"{BILLING}/v1/invoices", "inputSchema": {"type": "object", "properties": {
                 "order_id": {"type": "string"}}, "required": ["order_id"]}}


@pytest.fixture
def read_spec(monkeypatch):
    from core.native_builder import own_api

    async def read(spec):
        return {"tools": [SPEC_TOOL], "base": f"{BILLING}/v1", "problem": "",
                "auth": {"in": "header", "name": "X-Api-Key", "scheme": ""}}

    monkeypatch.setattr(own_api, "read_spec", read)
    return {own_api.SPECS_KEY: {"ledgerline": {"name": "Ledgerline", "spec": f"{BILLING}/openapi.json"}}}


async def test_a_new_tool_from_a_spec_operation_is_filled_by_code(client, read_spec):
    run = await _run("Add Ledgerline's invoice call as a tool", **read_spec)
    out = await _submitted_in(run, [{"op": "create", "target": {"kind": "tool", "name": "ledgerline_create_invoice"},
                                     "fields": {"from": {"spec": "Ledgerline",
                                                         "operation": "ledgerline_create_invoice"}}}])
    [op] = out["ops"]
    assert op["op"] == "create_tool" and op["fields"]["url"] == f"{BILLING}/v1/invoices", out
    assert op["fields"]["method"] == "POST" and op["fields"]["key"] == {"header": "X-Api-Key", "scheme": ""}
    assert op["fields"]["input_schema"]["required"] == ["order_id"]


async def test_an_operation_the_spec_does_not_have_is_sent_back_with_its_operations(client, read_spec):
    run = await _run("Add Ledgerline's invoice call as a tool", **read_spec)
    out = await _submitted_in(run, [{"op": "create", "target": {"kind": "tool", "name": "x"},
                                     "fields": {"from": {"spec": "Ledgerline", "operation": "delete_everything"}}}])
    assert "ledgerline_create_invoice" in out.get("error", ""), out


# ── chat first ──────────────────────────────────────────────────────────────────────────────────────────────────────

def test_a_reply_typed_instead_of_the_form_answers_each_open_field():
    from core.native_builder.change_tools import ASKED_KEY, _with_answers

    state = {ASKED_KEY: [{"about": "what", "subject": "address of tool fetch_orders", "field": "url",
                          "question": "What address should it call?"},
                         {"about": "what", "subject": "method of tool fetch_orders", "field": "method",
                          "question": "Which method?"}],
             "change_clarify": {"answer": f"Use this: curl {BILLING}/orders"}}
    answered = _with_answers(state)
    assert [a.get("answer") for a in answered] == [f"Use this: curl {BILLING}/orders"] * 2, answered


# ── what the planner is told ────────────────────────────────────────────────────────────────────────────────────────

def test_the_planner_is_told_where_a_tools_facts_come_from():
    from pathlib import Path

    from core.native_builder.change_tools import SUBMIT

    told = SUBMIT["function"]["description"]
    assert "from: {curl: n}" in told and "{spec, operation}" in told, told
    rules = json.loads((Path(__file__).resolve().parents[2] / "core" / "native_builder" / "agents" /
                        "planner.json").read_text())["system_prompt"]
    changing = rules.split("\n# Changing", 1)[1].split("\n# ", 1)[0]
    rule = next(line for line in changing.splitlines() if "`from`" in line)
    for word in ("curl", "spec", "docs", "research_questions", "{{ }}"):
        assert word in rule, (word, rule)
