"""Synapse AI makes and changes website widgets from a card (F1262, REACH2; D340 R3, Naveen 10-30: "create chat
widgets").

A widget is made, changed, turned off, deleted, given a new key or a logo through the Widgets screen's own code
(`core.chat_widgets.service`), on a card the person presses, so every door says the same thing for the same reason.
What a card changes is shown as it stands and as it will be (the shared rows, `native_builder.rows`). A visitor is a
stranger: a new widget's visitor tools are none unless the person named them, a tool the agent can't reach is refused
by name, and a tool that acts has its own line. The made widget's snippet is the press's answer. In the app only, and a
builder's, as the screen is.
"""
from __future__ import annotations

import json

import pytest

from core import builder_access
from core.builder_tools import execute_builder_tool_with_events
from core.chat_widgets import service, store
from core.models_orchestration import OrchestrationRun
from core.orchestration.state import SharedState
from core.product_tools import confirm, hooks
from core.product_tools import widgets as W
from core.product_tools.roles import Role


class _Server:
    agent_sessions: dict = {}
    tool_router: dict = {}


@pytest.fixture(autouse=True)
async def assistant_run():
    from core.store import get_store

    await get_store()
    confirm.set_claim_store(None)
    builder_access.forget_runs()
    await SharedState(OrchestrationRun(run_id="builder_wgt", orchestration_id="orch_native_builder",
                                       shared_state={builder_access.CALLER_KEY: {"user_id": None}})).checkpoint()
    yield
    hooks.set_role_source(None)
    W.set_widget_host(None)
    builder_access.forget_runs()


def _choices(tools: dict[str, str]):
    """The tools a visitor's turn on any agent reaches, by name and category: what the Widgets screen offers."""
    async def choices(agent_id: str) -> list[dict]:
        return [{"name": n, "category": c} for n, c in tools.items()]
    W.set_widget_host(W.WidgetHost(load=service.list_widgets, choices=choices))


async def _prepare(args):
    raw, events = await execute_builder_tool_with_events("prepare_action", args, _Server(), run_id="builder_wgt")
    return json.loads(raw), [e for e in events if e.get("type") == "action_needed"]


async def _press(client, card, i=0):
    res = await client.post("/api/assistant/act", json=card["buttons"][i])
    assert res.status_code == 200, res.text
    return res.json()["result"]


async def _agents(*agents: dict) -> list[dict]:
    """Every agent at once: a seed replaces the workspace's agents."""
    from _fakes.seed import make_agent, seed_agents

    return await seed_agents([make_agent(**a) for a in agents])


def _row(card, label):
    return next(r for r in card["rows"] if r["label"] == label)


async def test_a_widget_is_made_from_a_card_with_its_settings_shown_and_its_snippet_after(client, seed_agent):
    agent = await seed_agent(name="Front desk")
    _choices({"search_faq": "read_only"})

    said, [card] = await _prepare({"action": "widget_create", "name": "Support", "agent": "front desk",
                                   "sites": ["https://acme.com"], "greeting": "Hi there", "daily_cap": 200})
    assert card["title"] == 'Make a website widget "Support"' and "error" not in said
    assert _row(card, "Answers as") == {"label": "Answers as", "before": None, "after": "Front desk"}
    assert _row(card, "Sites")["after"] == "https://acme.com"
    assert _row(card, "Visitors may use")["after"] == "none"
    assert _row(card, "Messages a day")["after"] == "200" and _row(card, "Greeting")["after"] == "Hi there"
    assert await store.list_widgets() == []  # shown, not done

    made = await _press(client, card)
    [widget] = await store.list_widgets()
    assert widget["agent_id"] == agent["id"] and widget["allowed_origins"] == ["https://acme.com"]
    assert widget["visitor_tools"] == [] and widget["daily_message_cap"] == 200
    # The snippet's key is the press's answer; nothing the card already showed comes back.
    assert made == {"id": widget["id"], "name": "Support", "public_key": widget["public_key"], "enabled": True}


async def test_a_new_widget_s_visitor_tools_are_none_unless_named(client, seed_agent):
    """Never the agent's whole list (TRI1, F1262): a visitor is a stranger on someone's site."""
    await seed_agent(name="Front desk", tools=["search_faq", "send_email"])
    _choices({"search_faq": "read_only", "send_email": "open_world"})

    _, [card] = await _prepare({"action": "widget_create", "name": "Support", "agent": "Front desk"})
    assert card["buttons"][0]["arguments"]["visitor_tools"] == []
    assert "A visitor's turn" not in card["detail"]


async def test_a_visitor_tool_that_acts_has_its_own_line_and_one_out_of_reach_is_refused_by_name(seed_agent):
    await seed_agent(name="Front desk")
    _choices({"search_faq": "read_only", "send_email": "open_world", "delete_ticket": "destructive"})

    _, [card] = await _prepare({"action": "widget_create", "name": "Support", "agent": "Front desk",
                                "visitor_tools": ["search_faq", "send_email", "delete_ticket"]})
    lines = card["detail"].splitlines()
    assert "A visitor's turn can use send_email, which reaches outside this workspace." in lines
    assert "A visitor's turn can use delete_ticket, which deletes or overwrites." in lines
    assert not any("search_faq" in line for line in lines)  # a read is not an act
    assert _row(card, "Visitors may use")["after"] == "search_faq, send_email, delete_ticket"

    said, cards = await _prepare({"action": "widget_create", "name": "Support", "agent": "Front desk",
                                  "visitor_tools": ["run_bash"]})
    assert cards == [] and "can't reach run_bash" in said["error"]


async def test_a_visitor_tool_this_agent_asks_first_about_is_said_refused_with_the_two_ways_out(seed_agent):
    """F1524 (VID4's takes 1-2): a visitor can't approve, so on the site a tool the agent asks first about is refused.
    The card names each, with the two ways out, in place of the line that says what it does; a tool that runs
    without asking keeps its own line."""
    await seed_agent(name="Shop assistant")

    async def choices(agent_id: str) -> list[dict]:
        return [{"name": "search_books", "category": "read_only", "asks_first": False},
                {"name": "send_receipt", "category": "open_world", "asks_first": False},
                {"name": "create_order", "category": "open_world", "asks_first": True}]
    W.set_widget_host(W.WidgetHost(load=service.list_widgets, choices=choices))

    _, [card] = await _prepare({"action": "widget_create", "name": "Shop", "agent": "Shop assistant",
                                "visitor_tools": ["search_books", "send_receipt", "create_order"]})
    lines = card["detail"].splitlines()
    asked = [line for line in lines if "create_order" in line]
    assert len(asked) == 1, lines
    assert "asks first" in asked[0] and "can't approve" in asked[0] and "refused" in asked[0]
    assert "act without asking" in asked[0] and "leave it off" in asked[0]
    assert "A visitor's turn can use send_receipt, which reaches outside this workspace." in lines
    assert not any("search_books" in line for line in lines)


async def test_the_builder_s_own_agents_and_a_site_that_is_not_one_are_refused_in_words(seed_agent):
    await _agents({"id": "agent_native_builder_saver_create", "name": "Saver", "system": True},
                  {"name": "Front desk"})
    _choices({})

    said, cards = await _prepare({"action": "widget_create", "name": "Support", "agent": "Saver"})
    assert cards == [] and "No agent is called 'Saver'" in said["error"]

    said, cards = await _prepare({"action": "widget_create", "name": "Support", "agent": "Front desk",
                                  "sites": ["not a site"]})
    assert cards == [] and "not a site a widget can sit on" in said["error"]


async def test_a_change_names_only_what_changes_and_keeps_the_rest_and_the_key(client, seed_agent):
    agent = await seed_agent(name="Front desk")
    _choices({"search_faq": "read_only"})
    widget = await service.create({"name": "Support", "agent_id": agent["id"], "allowed_origins": ["https://acme.com"],
                                   "greeting": "Hello", "visitor_tools": ["search_faq"]})

    _, [card] = await _prepare({"action": "widget_update", "widget": "Support", "greeting": "Welcome back",
                                "sites": ["https://acme.com", "https://shop.acme.com"]})
    assert card["title"] == 'Change the website widget "Support"'
    assert card["rows"] == [
        {"label": "Sites", "before": "https://acme.com", "after": "https://acme.com, https://shop.acme.com"},
        {"label": "Greeting", "before": "Hello", "after": "Welcome back"},
    ]
    await _press(client, card)
    after = await store.get_widget(widget["id"])
    assert after["greeting"] == "Welcome back" and after["visitor_tools"] == ["search_faq"]
    assert after["public_key"] == widget["public_key"] and after["agent_id"] == agent["id"]

    said, cards = await _prepare({"action": "widget_update", "widget": "Support", "greeting": "Welcome back"})
    assert cards == [] and "already set that way" in said["error"]

    _, [card] = await _prepare({"action": "widget_update", "widget": "Support", "on": False})
    assert card["rows"] == [{"label": "On", "before": "on", "after": "off"}]
    await _press(client, card)
    assert (await store.get_widget(widget["id"]))["enabled"] is False


async def test_a_new_agent_keeps_the_visitor_tools_only_if_it_reaches_them():
    first, _ = await _agents({"name": "Front desk"}, {"name": "Sales"})
    reach = {"Front desk": {"search_faq": "read_only"}, "Sales": {}}
    names = {first["id"]: "Front desk"}

    async def choices(agent_id: str) -> list[dict]:
        return [{"name": n, "category": c} for n, c in reach[names.get(agent_id, "Sales")].items()]
    W.set_widget_host(W.WidgetHost(load=service.list_widgets, choices=choices))
    await service.create({"name": "Support", "agent_id": first["id"], "visitor_tools": ["search_faq"]})

    said, cards = await _prepare({"action": "widget_update", "widget": "Support", "agent": "Sales"})
    assert cards == [] and "can't reach search_faq" in said["error"]
    _, [card] = await _prepare({"action": "widget_update", "widget": "Support", "agent": "Sales", "visitor_tools": []})
    assert _row(card, "Answers as") == {"label": "Answers as", "before": "Front desk", "after": "Sales"}


async def test_a_delete_and_a_new_key_are_danger_cards_and_the_new_key_is_the_answer(client, seed_agent):
    agent = await seed_agent(name="Front desk")
    _choices({})
    widget = await service.create({"name": "Support", "agent_id": agent["id"], "allowed_origins": ["https://acme.com"]})

    _, [card] = await _prepare({"action": "widget_rotate_key", "widget": "Support"})
    assert card["danger"] is True and "old snippet stops working" in card["detail"]
    rotated = await _press(client, card)
    assert rotated["public_key"] != widget["public_key"]
    assert rotated["public_key"] == (await store.get_widget(widget["id"]))["public_key"]

    _, [card] = await _prepare({"action": "widget_delete", "widget": "Support"})
    assert card["danger"] is True and "https://acme.com" in card["detail"]
    await _press(client, card)
    assert await store.list_widgets() == []


async def test_a_logo_comes_from_an_image_in_the_vault_and_comes_off(client, seed_agent, tmp_path, monkeypatch):
    from core.routes import vault

    monkeypatch.setattr(vault, "_vault_root", lambda: tmp_path)
    monkeypatch.setattr(vault, "_get_storage", lambda source="auto": None)
    (tmp_path / "brand").mkdir()
    (tmp_path / "brand" / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 64)
    (tmp_path / "brand" / "notes.txt").write_text("not an image")
    agent = await seed_agent(name="Front desk")
    _choices({})
    widget = await service.create({"name": "Support", "agent_id": agent["id"]})

    _, [card] = await _prepare({"action": "widget_logo", "widget": "Support", "path": "brand/logo.png"})
    assert card["rows"] == [{"label": "Logo", "before": None, "after": "logo.png"}]
    await _press(client, card)
    assert (await store.get_widget(widget["id"]))["logo"].startswith("data:image/png;base64,")

    _, [card] = await _prepare({"action": "widget_logo", "widget": "Support", "path": "brand/notes.txt"})
    res = await client.post("/api/assistant/act", json=card["buttons"][0])
    assert res.status_code != 200 and "not a PNG, JPEG, GIF or WebP" in res.text

    _, [card] = await _prepare({"action": "widget_logo_remove", "widget": "Support"})
    await _press(client, card)
    assert (await store.get_widget(widget["id"]))["logo"] == ""


async def test_a_viewer_gets_no_card(seed_agent):
    async def source(user_id):
        return Role.VIEWER

    hooks.set_role_source(source)
    await seed_agent(name="Front desk")
    _choices({})
    await SharedState(OrchestrationRun(run_id="builder_wgt", orchestration_id="orch_native_builder",
                                       shared_state={builder_access.CALLER_KEY: {"user_id": "v1"}})).checkpoint()
    builder_access.forget_runs()
    said, cards = await _prepare({"action": "widget_create", "name": "Support", "agent": "Front desk"})
    assert cards == [] and "builder" in said["error"]


def test_the_widget_tools_are_the_app_s_own_and_a_builder_s():
    from core.product_tools import areas, registry

    areas.ensure_loaded()
    for name in ("create_widget", "update_widget", "delete_widget", "rotate_widget_key", "set_widget_logo"):
        tool = registry.get(name)
        assert tool.app_only and tool.min_role == Role.BUILDER and tool.kind == "act", name
    assert registry.get("delete_widget").irreversible
    assert registry.get("rotate_widget_key").annotations()["destructiveHint"]  # F1548: an act is, unless it only adds


@pytest.mark.parametrize("op,fields,words", [
    ("widget_create", {"sites": ["https://acme.com"]}, "needs agent"),
    ("widget_update", {"theme": "neon"}, "`theme` is one of auto, light, dark"),
    ("widget_update", {"on": "yes"}, "`on` is true or false"),
    ("widget_update", {"daily_cap": -1}, "`daily_cap` is a whole number"),
    ("widget_update", {"sites": "https://acme.com"}, "`sites` is a list"),
    ("widget_update", {"public_key": "pk_x"}, "cannot set public_key"),
])
def test_a_widget_op_is_checked_before_its_card(op, fields, words):
    from core.native_builder import change_ops as C

    found = C.issues(op, {"kind": "widget", "name": "Support"}, fields)
    assert any(words in f for f in found), found


def test_the_widgets_screen_is_reached_and_not_said_as_out_of_reach():
    from core import feature_census

    row = next(s for s in feature_census.CENSUS if s.id == "widgets")
    assert row.cant == "" and {"create_widget", "update_widget", "delete_widget"} <= set(row.tools)


async def test_the_screen_s_routes_say_what_they_said(client, seed_agent):
    """One place (F1189): the routes call the service and answer as before."""
    builder = await seed_agent(id="agent_native_builder_saver_create", name="Saver", system=True)
    res = await client.post("/api/widgets", json={"name": "X", "agent_id": builder["id"]})
    assert res.status_code == 409 and "cannot be published on a widget" in res.json()["detail"]
    assert (await client.put("/api/widgets/wgt_nope", json={"name": "X"})).status_code == 404
    assert (await client.delete("/api/widgets/wgt_nope")).json()["detail"] == "No such widget."
