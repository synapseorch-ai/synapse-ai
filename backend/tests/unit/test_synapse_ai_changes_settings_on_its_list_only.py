"""Synapse AI changes settings on its allow-list only, through a reviewed change with undo (F1264, REACH1; D340 R5).

Naveen, 10-30: "change some settings", on a safe allow-list, each through an approval card; never keys, billing, the
plan, members, API keys, the MCP server or import and export (those get a link). D292 stands: in the app only, an
owner or admin, with undo like any change. These pin:

- every field of the Settings model is placed, in exactly one of `ALLOWED` and `OUT`;
- code refuses a setting off the list by name, with where it is done, and a value off its type or bounds; keys,
  billing and the plan, members and roles, API keys, the MCP server and import and export each with its link;
- the review says each setting as it reads now and as it will, and its op carries the same as rows for the card
  (REACH2's one shape); the yes saves it through the Settings route; undo puts back what it read, "automatic" too;
- a builder is refused (an admin's change), and the tool is never offered off the app;
- a host's own list narrows what Synapse AI may set, never widens it;
- topic `settings` reads each setting on the list, and never a key.
"""
from __future__ import annotations

import json

import pytest

from core import builder_access, settings_allow
from core.builder_tools import execute_builder_tool_with_events
from core.models import Settings
from core.models_orchestration import OrchestrationRun
from core.native_builder import change_ops as C
from core.orchestration.engine import live
from core.orchestration.state import SharedState
from core.product_tools import confirm, hooks
from core.product_tools.roles import Role

KEY = "sk-" + "proj-" + "S" * 40


class _Server:
    agent_sessions: dict = {}
    tool_router: dict = {}


@pytest.fixture(autouse=True)
def settings_from_the_store():
    from core import settings_runtime
    from core.config import get_settings_provider, set_settings_provider

    previous = get_settings_provider()
    settings_runtime.reset_state()
    settings_runtime.install_provider()
    confirm.set_claim_store(None)
    builder_access.forget_runs()
    yield
    settings_allow.set_writable(None)
    hooks.set_role_source(None)
    builder_access.forget_runs()
    settings_runtime.reset_state()
    set_settings_provider(previous)


def _as(role: Role):
    async def source(user_id):
        return role

    hooks.set_role_source(source)


_N = {"n": 0}


async def _call(tool, args, run):
    with live(run):
        raw, _ = await execute_builder_tool_with_events(tool, args, _Server(), run_id=run.run_id)
    return json.loads(raw)


async def _change(draft):
    _N["n"] += 1
    run = OrchestrationRun(run_id=f"builder_f1264_{_N['n']}", orchestration_id="orch_native_builder",
                           shared_state={builder_access.CALLER_KEY: {"user_id": "u1"}})
    await SharedState(run).checkpoint()
    checked = await _call("check_change", {"draft": {"finish": "submit_change", **draft}}, run)
    if checked["outcome"] != "review":
        return run, checked, None
    run.shared_state["change_checked"] = json.dumps(checked)
    run.shared_state["manage_review"] = {"feedback": "approve"}
    await _call("approve_change", {}, run)
    return run, checked, await _call("apply_change", {}, run)


def _settings(**fields):
    return {"op": "settings_set", "target": {"kind": "settings", "id": "workspace"}, "fields": fields}


class TestTheList:
    def test_every_setting_is_placed_once(self):
        names = set(Settings.model_fields)
        assert set(settings_allow.ALLOWED) | set(settings_allow.OUT) == names
        assert not set(settings_allow.ALLOWED) & set(settings_allow.OUT)

    @pytest.mark.parametrize("name", ["openai_key", "bash_allowed_dirs", "login_password_hash", "embedding_model",
                                      "run_notification_channel_id", "allow_stdio_mcp"])
    def test_what_is_off_it_is_refused_by_name_with_where(self, name):
        found = C.issues("settings_set", {"kind": "settings", "id": "workspace"}, {name: "x"})
        assert len(found) == 1 and f"`{name}`" in found[0] and "changed at /" in found[0]

    @pytest.mark.parametrize("name, link", [("api_keys", "/settings/api_keys"), ("api_key", "/settings/api_keys"),
                                            ("mcp_server", "/settings/mcp_server"),
                                            ("import_export", "/settings/import_export"),
                                            ("export", "/settings/import_export")])
    def test_what_is_never_synapse_ais_is_refused_with_its_link(self, name, link):
        """Naveen, 10-30: never API keys, the workspace's MCP server or import and export: those get a link."""
        [found] = C.issues("settings_set", {"kind": "settings", "id": "workspace"}, {name: True})
        assert f"`{name}`" in found and f"It is changed at {link}." in found

    @pytest.mark.parametrize("name", ["billing", "plan", "members", "roles", "role", "subscription"])
    def test_billing_the_plan_members_and_roles_are_refused_and_a_host_gives_their_link(self, name):
        """Self-hosted has no such screens, and says so; a host that has them (cloud) links them."""
        [found] = C.issues("settings_set", {"kind": "settings", "id": "workspace"}, {name: "pro"})
        assert f"`{name}`" in found and "no such screen" in found
        settings_allow.set_links({"billing": "/settings/billing", "plan": "/settings/billing",
                                  "members": "/settings/members", "roles": "/settings/members"})
        try:
            [linked] = C.issues("settings_set", {"kind": "settings", "id": "workspace"}, {name: "pro"})
        finally:
            settings_allow.set_links(None)
        assert "It is changed at /settings/" in linked

    async def test_the_tool_itself_refuses_them_too(self, client):
        from core.product_tools import settings

        found = await settings.issues({"api_keys": "new", "members": ["a"], "openai_key": KEY})
        assert len(found) == 3 and all("is changed at" in f or "no such screen" in f for f in found)

    def test_no_key_holder_is_on_it(self):
        from core.config import SECRET_SETTINGS

        assert not set(SECRET_SETTINGS) & set(settings_allow.ALLOWED)

    @pytest.mark.parametrize("name, value, said", [
        ("reasoning_effort", "extreme", "one of off, low, medium, high"),
        ("auto_compact_window_pct", 95, "from 0 to 90"),
        ("prompt_cache_enabled", "yes", "true or false"),
        ("team_at_once_ceiling", 2.5, "whole number"),
        ("tool_policy_defaults", {"destructive": "maybe"}, "allow, confirm, deny"),
    ])
    def test_a_value_off_its_type_or_bounds_is_refused(self, name, value, said):
        [found] = C.issues("settings_set", {"kind": "settings", "id": "workspace"}, {name: value})
        assert said in found

    def test_automatic_is_a_value(self):
        assert C.issues("settings_set", {"kind": "settings", "id": "workspace"}, {"max_output_tokens": None}) == []

    def test_its_call_is_the_settings_tool_with_exactly_those(self):
        assert C.call(C.OPS["settings_set"], "workspace", {"prompt_cache_enabled": False}, {}) == (
            "set_settings", {"changes": {"prompt_cache_enabled": False}})


class TestAChange:
    async def test_it_is_reviewed_before_and_after_saved_and_undone(self, client):
        from core.config import load_settings
        from core.routes.settings import update_settings

        _as(Role.OWNER)
        await update_settings(Settings(prompt_cache_enabled=True, auto_compact_window_pct=50))
        first, checked, done = await _change({"ops": [_settings(prompt_cache_enabled=False,
                                                                auto_compact_window_pct=30)]})
        # One shape wherever a yes is asked (REACH2's rows): the review's lines and the card's rows say the same.
        assert checked["review"] == ("- Change the workspace's settings:\n"
                                     "  Prompt caching: on → off\n"
                                     "  Compact at (% of the model's window): 50 → 30")
        [op] = [o for o in checked["ops"] if o["op"] == "settings_set"]
        assert op["rows"] == [{"label": "Prompt caching", "before": "on", "after": "off"},
                              {"label": "Compact at (% of the model's window)", "before": "50", "after": "30"}]
        assert done["applied"] == ["settings_set"], done
        now = load_settings()
        assert now["prompt_cache_enabled"] is False and now["auto_compact_window_pct"] == 30
        await _change({"undo_of": first.run_id})
        back = load_settings()
        assert back["prompt_cache_enabled"] is True and back["auto_compact_window_pct"] == 50

    async def test_a_builder_is_refused(self, client):
        _as(Role.BUILDER)
        _, checked, done = await _change({"ops": [_settings(prompt_cache_enabled=False)]})
        assert done is None and "needs the admin role" in json.dumps(checked)

    async def test_a_model_is_one_the_workspace_can_run(self, client, monkeypatch):
        from core.native_builder import models_menu

        async def refused(model, before=None, **kw):
            return f'"{model}" is not a model this workspace can use.', None

        monkeypatch.setattr(models_menu, "model_refusal", refused)
        _as(Role.OWNER)
        _, checked, done = await _change({"ops": [_settings(builder_model="no-such-model")]})
        assert done is None and "is not a model this workspace can use" in json.dumps(checked)

    async def test_a_key_in_a_value_is_never_kept(self, client):
        _as(Role.OWNER)
        _, checked, done = await _change({"ops": [_settings(agent_name=KEY)]})
        assert done is None and KEY not in json.dumps(checked)


class TestAHost:
    async def test_its_list_narrows_what_may_be_set(self, client):
        settings_allow.set_writable(lambda: {"model", "prompt_cache_enabled"})
        assert set(settings_allow.allowed()) == {"model", "prompt_cache_enabled"}
        [found] = C.issues("settings_set", {"kind": "settings", "id": "workspace"}, {"team_at_once_ceiling": 5})
        assert "not changed from Synapse AI on this workspace" in found

    def test_it_never_widens_it(self):
        settings_allow.set_writable(lambda: {"model", "openai_key"})
        assert set(settings_allow.allowed()) == {"model"}


def test_the_tool_is_an_admins_in_the_app_only():
    from core.product_tools import areas, registry
    from core.product_tools.caller import Caller

    areas.ensure_loaded()
    tool = registry.get("set_settings")
    assert tool.app_only and tool.min_role == Role.ADMIN
    assert not tool.offered_to(Caller(role=Role.OWNER, via="mcp"))
    assert not tool.offered_to(Caller(role=Role.OWNER, via="api"))
    assert not tool.offered_to(Caller(role=Role.BUILDER, via="assistant"))


async def test_the_settings_topic_reads_the_list_and_never_a_key(client):
    from core.routes.settings import update_settings

    await update_settings(Settings(openai_key=KEY, prompt_cache_enabled=False))
    raw, _ = await execute_builder_tool_with_events("read_workspace", {"topic": "settings"}, _Server())
    got = json.loads(raw)
    assert KEY not in raw
    by_name = {i["name"]: i for i in got["items"]}
    assert set(by_name) == set(settings_allow.ALLOWED)
    assert by_name["prompt_cache_enabled"]["value"] == "off"
    assert "API keys" in got["note"] and "/settings/" in got["note"]
