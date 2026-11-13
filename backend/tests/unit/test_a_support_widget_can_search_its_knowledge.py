"""A support widget can search its knowledge, and a card says so when it can't (F1570).

VID7 (11-13, a scratch 2.0.8) asked Synapse AI to "Put it on Harborpine's site as a chat widget called Harborpine help,
visitors may only search the help pages". The card read "Visitors may use: none", twice: the plan held
`visitor_tools: []` ("no visitor tools (search only)"), since visitor tools were "only those the person named", by
their tool names. A visitor's turn searches an agent's knowledge only through `search_knowledge` and `read_knowledge`
over the bases opened to visitors, so the flagship support bot was made unable to read its own help pages, and nothing
said so.

The class: what a person asks a visitor to be able to do, mapped onto the visitor tools that do it, by the server, and
said when it isn't so.

* A knowledge base the agent answers from, named among a widget's visitor tools (or the word "knowledge"), is its two
  knowledge tools; one not open to visitors is refused with how to open it.
* A name is never dropped unsaid: a memory or board tool, never a visitor's, is refused by name.
* A widget (made, or given another agent) or a channel whose senders are visitors (given other agents), on an agent
  that answers from knowledge its visitors can't search, says so on its card, with what to do.
"""
from __future__ import annotations

import json

import pytest

from _fakes import seed as S
from _fakes.knowledge_corpus import CONTRACTS, REPORTS, save_bases
from core import builder_access
from core.builder_tools import execute_builder_tool_with_events
from core.chat_widgets import store as widget_store
from core.messaging import store as channel_store
from core.models_orchestration import OrchestrationRun
from core.orchestration.state import SharedState
from core.product_tools import confirm, hooks
from core.product_tools import widgets as W

HELP = CONTRACTS.model_copy(update={"name": "Help pages", "visitor_access": True})
HELP_CLOSED = CONTRACTS.model_copy(update={"name": "Help pages"})


class _Server:
    agent_sessions: dict = {}
    tool_router: dict = {}


@pytest.fixture(autouse=True)
async def assistant_run(client, monkeypatch):
    import core.tools as tools_mod
    from core.store import get_store

    async def aggregated(server_module, agent, custom_tools, **_):
        return [], {}, [], ""

    # Only Knowledge's tools are a visitor's to reach here.
    monkeypatch.setattr(tools_mod, "aggregate_all_tools", aggregated)
    await get_store()
    assert (await client.post("/api/settings", json={"knowledge_enabled": True})).status_code == 200
    confirm.set_claim_store(None)
    builder_access.forget_runs()
    await SharedState(OrchestrationRun(run_id="builder_kw", orchestration_id="orch_native_builder",
                                       shared_state={builder_access.CALLER_KEY: {"user_id": None}})).checkpoint()
    yield
    hooks.set_role_source(None)
    W.set_widget_host(None)
    builder_access.forget_runs()


async def _prepare(args):
    raw, events = await execute_builder_tool_with_events("prepare_action", args, _Server(), run_id="builder_kw")
    return json.loads(raw), [e for e in events if e.get("type") == "action_needed"]


async def _support(*bases, knowledge=None, **extra) -> dict:
    await save_bases(*bases)
    agent = S.make_agent(name="Harborpine Support", knowledge=knowledge if knowledge is not None
                         else [b.id for b in bases], **extra)
    other = S.make_agent(name="Front desk")
    await S.seed_agents([agent, other])
    return agent


def _row(card, label):
    return next(r for r in card["rows"] if r["label"] == label)


# ── what a person names ──────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("named", ["Help pages", "help pages", "knowledge"])
async def test_a_base_named_for_visitors_is_their_knowledge_search(named):
    await _support(HELP)
    said, cards = await _prepare({"action": "widget_create", "name": "Harborpine help", "agent": "Harborpine Support",
                                  "sites": ["https://harborpine.com"], "visitor_tools": [named]})
    assert "error" not in said, said
    [card] = cards
    assert card["buttons"][0]["arguments"]["visitor_tools"] == ["search_knowledge", "read_knowledge"]
    assert _row(card, "Visitors may use")["after"] == "search_knowledge, read_knowledge"
    assert "can't search" not in card["detail"]


async def test_a_base_not_open_to_visitors_is_refused_with_how_to_open_it():
    await _support(HELP_CLOSED)
    said, cards = await _prepare({"action": "widget_create", "name": "Harborpine help", "agent": "Harborpine Support",
                                  "visitor_tools": ["Help pages"]})
    assert cards == []
    assert '"Help pages" is not open to visitors' in said["error"], said
    assert "Visitors may search it" in said["error"]


async def test_a_name_no_visitor_may_hold_is_refused_by_name_not_dropped():
    await _support(HELP)
    said, cards = await _prepare({"action": "widget_create", "name": "Harborpine help", "agent": "Harborpine Support",
                                  "visitor_tools": ["search_knowledge", "remember"]})
    assert cards == []
    assert "remember" in said["error"] and "never a visitor's" in said["error"], said


async def test_an_unknown_name_says_how_to_let_visitors_search():
    await _support(HELP)
    said, cards = await _prepare({"action": "widget_create", "name": "Harborpine help", "agent": "Harborpine Support",
                                  "visitor_tools": ["search the help pages"]})
    assert cards == []
    assert "search the help pages" in said["error"] and "Help pages" in said["error"], said


# ── what a card says when visitors can't search ───────────────────────────────────────────────────────────────────────

async def test_a_widget_whose_visitors_cant_search_its_agents_knowledge_says_so():
    """VID7's card: the plan named no visitor tools."""
    await _support(HELP)
    _, [card] = await _prepare({"action": "widget_create", "name": "Harborpine help", "agent": "Harborpine Support",
                                "sites": ["https://harborpine.com"], "visitor_tools": []})
    assert _row(card, "Visitors may use")["after"] == "none"
    line = next((x for x in card["detail"].splitlines() if "can't search" in x), "")
    assert "Harborpine Support" in line and "Help pages" in line, card["detail"]


async def test_and_says_to_open_a_base_first_when_none_is_open_to_visitors():
    await _support(HELP_CLOSED)
    _, [card] = await _prepare({"action": "widget_create", "name": "Harborpine help", "agent": "Harborpine Support"})
    line = next((x for x in card["detail"].splitlines() if "can't search" in x), "")
    assert "Help pages" in line and "Visitors may search it" in line, card["detail"]


async def test_some_bases_left_closed_are_named():
    await _support(HELP, REPORTS)
    _, [card] = await _prepare({"action": "widget_create", "name": "Harborpine help", "agent": "Harborpine Support",
                                "visitor_tools": ["search_knowledge"]})
    line = next((x for x in card["detail"].splitlines() if "Reports" in x), "")
    assert "not open to visitors" in line, card["detail"]
    assert "Help pages" not in line


async def test_a_widget_whose_visitors_can_search_says_nothing_more():
    await _support(HELP)
    _, [card] = await _prepare({"action": "widget_create", "name": "Harborpine help", "agent": "Harborpine Support",
                                "visitor_tools": ["search_knowledge", "read_knowledge"]})
    assert "can't search" not in card["detail"] and "not open to visitors" not in card["detail"]


async def test_an_agent_without_knowledge_says_nothing_about_it():
    await _support(HELP)
    _, [card] = await _prepare({"action": "widget_create", "name": "Desk", "agent": "Front desk"})
    assert "can't search" not in card["detail"]


async def test_a_widget_given_an_agent_with_knowledge_says_so():
    await _support(HELP)
    from core.store.resources import load_agents

    front = next(a for a in await load_agents() if a["name"] == "Front desk")
    await widget_store.save_widget({"name": "Desk", "agent_id": front["id"], "allowed_origins": [],
                                    "visitor_tools": []})
    _, [card] = await _prepare({"action": "widget_update", "widget": "Desk", "agent": "Harborpine Support"})
    line = next((x for x in card["detail"].splitlines() if "can't search" in x), "")
    assert "Harborpine Support" in line and "Help pages" in line, card["detail"]


# ── a channel whose senders are visitors ─────────────────────────────────────────────────────────────────────────────

async def _telegram(**extra):
    return await channel_store.save_channel({"name": "Support bot", "platform": "telegram", "mode": "polling",
                                             "credentials": {"bot_token": "123456:SECRET-TOKEN"}, **extra})


async def test_a_visitor_channel_given_an_agent_with_knowledge_says_so():
    await _support(HELP)
    await _telegram(senders_are_members=False, visitor_tools=[])
    _, [card] = await _prepare({"action": "channel_agents", "channel": "Support bot",
                                "agents": ["Harborpine Support"]})
    line = next((x for x in (card.get("detail") or "").splitlines() if "can't search" in x), "")
    assert "Harborpine Support" in line and "Help pages" in line, card.get("detail")


async def test_a_member_channel_says_nothing_about_it():
    """Members' turns hold the agent's own tools, its knowledge's among them."""
    await _support(HELP)
    await _telegram()
    _, [card] = await _prepare({"action": "channel_agents", "channel": "Support bot",
                                "agents": ["Harborpine Support"]})
    assert "can't search" not in (card.get("detail") or "")
