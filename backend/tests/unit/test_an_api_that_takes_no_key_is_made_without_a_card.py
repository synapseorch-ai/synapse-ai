"""
An API that takes no key is made as the research read it, with no Connect card (F1411, BLD1; the v2.0 media's clip 5,
11-06: the Frankfurter API, keyless, by its spec).

`report_findings`' api schema makes `auth` required, each of `in`, `name` and `scheme` with it, so a spec of an API
that takes no key comes as blanks: `{"in": "header", "name": "", "scheme": ""}`, as the clip's research sent. Before,
`check_api_spec` read that as "its auth is not a header with Bearer, Token or the key alone" and dropped the spec, and
the plan said "I can't build get_frankfurter_rates here yet". Now no auth (missing, `{}`, or a header with its name
and scheme both blank) is kept as an API that takes no key, the shape a person's own keyless API already has (F1225):
`"auth": None, "keyless": True`. The build then makes its tool after the yes, says "It takes no key." and puts up no
card. A spec that names an auth is held as before: a key in the URL or a Basic sign-in is no way in here yet, a key
with no header to send it in is no spec, and a key inside the spec drops it first.
"""
from __future__ import annotations

import json

import pytest

from _fakes import seed as S
from core import builder_access, builder_research as br, builder_tools
from core.builder_tools import execute_builder_tool_with_events
from core.models_orchestration import OrchestrationRun
from core.native_builder import build_plan, build_save
from core.orchestration.engine import live
from core.orchestration.state import SharedState
from core.product_tools import confirm, hooks
from core.product_tools.roles import Role

DOCS = "https://frankfurter.dev/"
BLANK = {"in": "header", "name": "", "scheme": ""}


def _spec(auth=BLANK, **more) -> dict:
    spec = {"base_url": "https://api.frankfurter.dev/v1",
            "endpoints": [{"method": "GET", "path": "/latest", "does": "Today's rates from one currency to others",
                           "tool": "frankfurter_latest_rates"}],
            "docs_url": DOCS, **more}
    return spec if auth is ... else {**spec, "auth": auth}


class _Server:
    agent_sessions: dict = {}
    tool_router: dict = {}


@pytest.fixture(autouse=True)
async def workspace(monkeypatch):
    br._LOOSE.clear()
    br._LOOSE[""] = {**br._fresh(), "read_hosts": ["frankfurter.dev"]}
    confirm.set_claim_store(None)
    builder_access.forget_runs()

    async def owner(user_id):
        return Role.OWNER

    hooks.set_role_source(owner)
    monkeypatch.setattr(build_plan, "MAKES_HTTP_TOOLS", True)
    await S.seed_agents([S.make_agent(id="agent_desk", name="Front desk", tools=[])])
    yield
    hooks.set_role_source(None)
    builder_access.forget_runs()
    builder_tools.set_room_check(None)
    br._LOOSE.clear()


async def _report(api) -> dict:
    option = {"provider": "Frankfurter", "how": "api", "detail": "Today's exchange rates, free and open",
              "needs": "No account, no key", "sources": [DOCS], "own_domains": ["frankfurter.dev"], "api": api}
    out = await br.report_findings({"goal": "price the beans in euros", "options": [option]}, None, [])
    [found] = out["options"]
    return found


async def _call(tool, args, run):
    with live(run):
        raw, events = await execute_builder_tool_with_events(tool, args, _Server(), run_id=run.run_id)
    return (json.loads(raw) if isinstance(raw, str) else raw), events


async def _approved(found: dict):
    run = OrchestrationRun(run_id="builder_rates", orchestration_id="orch_native_builder",
                           shared_state={builder_access.CALLER_KEY: {"user_id": "u1"}, "intent": "create",
                                         build_plan.FINDINGS_KEY: {"goal": "price the beans in euros",
                                                                   "options": [found]}})
    await SharedState(run).checkpoint()
    draft = {"finish": "submit_plan", "kind": "create", "title": "Bean prices", "summary": "Today's rates in euros.",
             "steps": [{"id": "rates", "type": "tool", "name": "Read today's rates", "does": "Reads GBP and USD to EUR",
                        "tool": "frankfurter_latest_rates"}]}
    run.shared_state[build_plan.DRAFT_KEY] = draft
    checked, _ = await _call("check_build", {"draft": draft}, run)
    run.shared_state[build_plan.CHECKED_KEY] = checked
    assert "build" in checked["offers"], checked["say"]
    run.shared_state["review_feedback"] = {"feedback": "Build it"}
    approved, _ = await _call("approve_plan", {}, run)
    run.shared_state["plan_approval"] = approved
    return run, checked


async def _tool(name="frankfurter_latest_rates"):
    from core.store.resources import load_tools

    return next((t for t in await load_tools() if t["name"] == name), None)


def _cards(events: list[dict]) -> list[dict]:
    return [e for e in events if "tool_secret" in json.dumps(e, default=str)]


@pytest.mark.parametrize("auth", [BLANK, {"name": "", "scheme": ""}, {}, None, ...],
                         ids=["blank-header", "blank-no-in", "empty", "none", "missing"])
async def test_a_spec_with_no_auth_is_kept_as_taking_no_key(auth):
    found = await _report(_spec(auth))
    assert found["how"] == "api" and "not used" not in found["detail"], found
    assert found["api"]["auth"] is None and found["api"]["keyless"] is True
    assert found["api"]["base_url"] == "https://api.frankfurter.dev/v1"
    assert [e["tool"] for e in found["api"]["endpoints"]] == ["frankfurter_latest_rates"]


async def test_its_tool_is_made_after_the_yes_with_no_card_and_no_wait(client):
    run, checked = await _approved(await _report(_spec()))
    assert "`frankfurter_latest_rates`: GET /latest on api.frankfurter.dev (Frankfurter). It takes no key." \
        in checked["review"]
    made, events = await _call("save_build", {"only": "tools"}, run)
    assert made["outcome"] == "tools_made" and made["tools"] == ["frankfurter_latest_rates"], made
    assert made["waits"] == [] and not _cards(events), (made, events)
    tool = await _tool()
    assert tool["base_lock"] == "https://api.frankfurter.dev/v1" and not tool.get("headers")
    saved, _ = await _call("save_build", {}, run)
    assert saved["outcome"] == "saved", saved


async def test_a_spec_that_names_its_header_still_asks_for_its_key_on_a_card(client):
    found = await _report(_spec({"in": "header", "name": "X-Api-Key", "scheme": ""}))
    assert found["api"]["auth"] == {"in": "header", "name": "X-Api-Key", "scheme": ""}
    assert "keyless" not in found["api"]
    run, checked = await _approved(found)
    assert "You give it your key on a card after your yes." in checked["review"]
    made, events = await _call("save_build", {"only": "tools"}, run)
    assert made["waits"] == ["Frankfurter"] and _cards(events), (made, events)


@pytest.mark.parametrize("auth, said", [
    ({"in": "query", "name": "api_key", "scheme": ""}, "takes its key in the URL"),
    ({"in": "header", "name": "Authorization", "scheme": "Basic"}, "username and password"),
])
async def test_a_key_in_the_url_or_a_sign_in_is_still_no_way_in(auth, said):
    found = await _report(_spec(auth))
    assert found["how"] == "unclear" and said in found["detail"] and "api" not in found


@pytest.mark.parametrize("auth", [{"in": "header", "name": "", "scheme": "Bearer"},
                                  {"in": "header", "name": "", "scheme": "Token"}, "none", ["header"]],
                         ids=["bearer-no-header", "token-no-header", "a-string", "a-list"])
async def test_a_key_with_no_header_to_send_it_in_is_still_no_spec(auth):
    found = await _report(_spec(auth))
    assert found["how"] == "api" and "api" not in found
    assert "its auth is not a header with Bearer, Token or the key alone" in found["detail"]


async def test_a_key_inside_a_keyless_spec_still_drops_it_first():
    found = await _report(_spec(example="Authorization: Bearer sk_live_" + "Z" * 24))
    assert "api" not in found and "it held a key" in found["detail"]
    assert "sk_live_" not in str(found)
