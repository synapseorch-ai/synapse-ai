"""A widget's site keeps the scheme the person typed (F1525, VID4's take 3).

Asked to "put it on our website, http://books.harborpine.localhost:18942", the planner wrote the site without its
scheme, and a bare host becomes https (`chat_widgets.origins`): the card said https and the widget never showed on the
http page. The person's words come before the model's retelling, so code reads the scheme they typed for that host
and port. A host they typed bare still defaults to https, and a public http:// they typed is refused in words, as the
Widgets screen refuses it, rather than silently becoming https.
"""
from __future__ import annotations

import json

import pytest

from core import builder_access
from core.builder_tools import execute_builder_tool_with_events
from core.chat_widgets import service
from core.models_orchestration import OrchestrationRun
from core.orchestration.engine import live
from core.orchestration.state import SharedState
from core.product_tools import confirm, hooks
from core.product_tools import widgets as W
from core.product_tools.roles import Role


class _Server:
    agent_sessions: dict = {}
    tool_router: dict = {}


@pytest.fixture(autouse=True)
async def fresh():
    from core.store import get_store

    await get_store()
    confirm.set_claim_store(None)
    builder_access.forget_runs()

    async def owner(user_id):
        return Role.OWNER
    hooks.set_role_source(owner)

    async def choices(agent_id: str) -> list[dict]:
        return []
    W.set_widget_host(W.WidgetHost(load=service.list_widgets, choices=choices))
    yield
    hooks.set_role_source(None)
    W.set_widget_host(None)
    builder_access.forget_runs()


async def _card(said: str, sites: list[str], seed_agent) -> tuple[dict, list[dict]]:
    """The change the planner proposes for `said`: a widget on "Shop assistant" with the sites as it wrote them."""
    await seed_agent(name="Shop assistant")
    run = OrchestrationRun(run_id="builder_site", orchestration_id="orch_native_builder",
                           shared_state={builder_access.CALLER_KEY: {"user_id": "u1"}, "user_message": said})
    await SharedState(run).checkpoint()
    ops = [{"op": "widget_create", "target": {"name": "Shop"},
            "fields": {"agent": "Shop assistant", "sites": sites}}]
    with live(run):
        raw, events = await execute_builder_tool_with_events(
            "check_change", {"draft": {"finish": "submit_change", "ops": ops}}, _Server(), run_id=run.run_id)
    return json.loads(raw), [e for e in events if e.get("type") == "action_needed"]


def _sites(cards: list[dict]) -> str:
    [card] = cards
    return next(r for r in card["rows"] if r["label"] == "Sites")["after"]


async def test_a_typed_http_localhost_keeps_its_http(seed_agent):
    _, cards = await _card("Put it on our website, http://localhost:3000, as a chat widget.", ["localhost:3000"],
                           seed_agent)
    assert _sites(cards) == "http://localhost:3000"


async def test_the_typed_scheme_wins_over_the_model_s(seed_agent):
    _, cards = await _card("Put it on http://localhost:3000 please.", ["https://localhost:3000"], seed_agent)
    assert _sites(cards) == "http://localhost:3000"


async def test_a_typed_public_http_site_is_refused_in_words_not_made_https(seed_agent):
    out, cards = await _card("Put it on http://shop.example.com", ["shop.example.com"], seed_agent)
    assert cards == []
    said = json.dumps(out)
    assert "http://shop.example.com" in said and "https://" in said


async def test_a_site_typed_bare_is_still_https(seed_agent):
    _, cards = await _card("Put it on shop.example.com", ["shop.example.com"], seed_agent)
    assert _sites(cards) == "https://shop.example.com"


def test_only_the_same_host_and_port_take_a_typed_scheme():
    from core.native_builder.change_ops import sites_as_typed

    words = "Our test page is http://localhost:3000, and the shop is at https://shop.example.com."
    assert sites_as_typed(["localhost:3000", "localhost:4000", "shop.example.com", "*.example.com"], words) == [
        "http://localhost:3000", "localhost:4000", "https://shop.example.com", "*.example.com"]
