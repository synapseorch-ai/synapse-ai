"""The product-tools layer the MCP server and the assistant share (MCP2, D292; ASSIST1, D291).

One registry decides who may call what — role, scope and surface — before any handler runs, asks for a
confirmation token where an act needs one, and tells the host's audit about every act it does. The adapters only
translate; none of these rules live in them.
"""
from __future__ import annotations

import pytest

from core.product_tools import confirm, hooks, page, registry
from core.product_tools.caller import Caller
from core.product_tools.registry import ProductTool, ToolRefusal, invoke
from core.product_tools.roles import Role

_SCHEMA = {"type": "object", "properties": {"id": {"type": "string"}}}


async def _echo(caller, args):
    return {"ok": True, "args": args, "who": caller.user_id}


def _tool(name="t_read", **kw) -> ProductTool:
    kw.setdefault("description", "a test tool")
    kw.setdefault("input_schema", _SCHEMA)
    kw.setdefault("handler", _echo)
    kw.setdefault("kind", "read")
    kw.setdefault("area", "test")
    return ProductTool(name=name, **kw)


def _act(name="t_act", **kw) -> ProductTool:
    kw.setdefault("kind", "act")
    kw.setdefault("scope", "run")
    kw.setdefault("min_role", Role.BUILDER)
    return _tool(name, **kw)


def _caller(role=Role.BUILDER, via="mcp", scopes=None, user_id="u1") -> Caller:
    return Caller(role=role, via=via, user_id=user_id, scopes=scopes)


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    """Each test registers into an empty registry and an empty claim store."""
    monkeypatch.setattr(registry, "_TOOLS", {})
    confirm.set_claim_store(None)
    hooks.set_audit(None)
    yield
    confirm.set_claim_store(None)
    hooks.set_audit(None)


@pytest.fixture
def audited():
    events: list[hooks.AuditEvent] = []

    async def record(event):
        events.append(event)

    hooks.set_audit(record)
    return events


class TestAnEntryIsCheckedWhenRegistered:
    def test_a_read_needs_no_more_than_the_read_scope(self):
        with pytest.raises(ValueError, match="read"):
            _tool(scope="run")

    def test_a_read_cannot_be_irreversible(self):
        with pytest.raises(ValueError, match="irreversible"):
            _tool(irreversible=True)

    def test_an_act_above_builder_must_stay_in_the_app(self):
        """Acts are capped at builder off the app (Naveen, D292): an admin act is app_only, by code."""
        with pytest.raises(ValueError, match="app_only"):
            _act(min_role=Role.ADMIN)
        assert _act(min_role=Role.ADMIN, app_only=True).app_only

    def test_a_name_an_mcp_client_would_refuse_is_refused(self):
        with pytest.raises(ValueError, match="name"):
            _tool("Bad Name")

    def test_two_entries_cannot_share_a_name(self):
        registry.register(_tool())
        with pytest.raises(ValueError, match="already"):
            registry.register(_tool())
        registry.register(_tool(description="replaced"), replace=True)
        assert registry.get("t_read").description == "replaced"


class TestAnnotationsComeFromTheFlags:
    def test_a_read(self):
        assert _tool().annotations() == {
            "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False,
        }

    def test_an_act_is_destructive_unless_it_only_adds(self):
        """F1548 (Naveen 11-12): an act that changes, replaces, stops or removes what is there is destructive, so a
        client asks its person first; only one that says it adds alone is not. MCP's own default for a tool that
        is not read-only, so a new act falls under the rule without anyone remembering it."""
        assert _act().annotations()["destructiveHint"] is True
        assert _act(additive=True).annotations()["destructiveHint"] is False
        assert _act(irreversible=True).annotations()["destructiveHint"] is True
        assert _act(idempotent=True).annotations()["idempotentHint"] is True
        assert _act(open_world=True).annotations()["openWorldHint"] is True

    def test_an_overwrite_or_a_stop_is_destructive_without_a_token(self):
        tool = _act()
        assert tool.annotations()["destructiveHint"] is True
        assert not tool.needs_token("irreversible")

    def test_only_an_act_can_say_it_only_adds_and_a_delete_cannot(self):
        with pytest.raises(ValueError, match="additive"):
            _tool(additive=True)
        with pytest.raises(ValueError, match="additive"):
            _act(additive=True, irreversible=True)


class TestEveryEntryHasATitle:
    """F1546 (DIR1): Claude's Connectors Directory takes a server only when every tool has a `title` a person
    reads (its listing, a client's permission prompt), and none had one. An entry says its title from its name
    unless it states one, so a new entry can never be without one."""

    def test_its_name_is_said_as_words(self):
        assert _tool("list_agents").title == "List agents"
        assert _act("chat_with_agent").title == "Chat with agent"

    def test_a_word_is_spelled_as_its_owner_spells_it(self):
        assert _act("create_http_tool").title == "Create HTTP tool"
        assert _act("test_python_tool").title == "Test Python tool"
        assert _act("import_openapi").title == "Import OpenAPI"
        assert _tool("list_db_configs").title == "List database configs"

    def test_a_stated_title_is_kept(self):
        assert _tool("get_tools_detail", title="Get tool details").title == "Get tool details"

    def test_a_host_that_rewraps_an_entry_keeps_its_title(self):
        """Cloud re-registers some entries with `dataclasses.replace` (its audit, its evals host)."""
        import dataclasses

        assert dataclasses.replace(_act("create_http_tool"), self_audited=True).title == "Create HTTP tool"


class TestWhoIsOfferedWhat:
    def test_a_viewer_sees_reads_only(self):
        registry.register(_tool())
        registry.register(_act())
        names = [t.name for t in registry.for_caller(_caller(Role.VIEWER))]
        assert names == ["t_read"]

    def test_an_approver_sees_what_an_approver_may_do(self):
        registry.register(_act("t_answer", min_role=Role.APPROVER))
        registry.register(_act())
        names = [t.name for t in registry.for_caller(_caller(Role.APPROVER))]
        assert names == ["t_answer"]

    def test_a_scope_not_granted_hides_the_tool(self):
        registry.register(_tool())
        registry.register(_act())
        names = [t.name for t in registry.for_caller(_caller(Role.OWNER, scopes=frozenset({"read"})))]
        assert names == ["t_read"]

    def test_an_app_only_entry_is_never_offered_over_mcp(self):
        registry.register(_tool("t_admin_read", app_only=True, min_role=Role.ADMIN))
        assert registry.for_caller(_caller(Role.OWNER, via="mcp")) == []
        assert [t.name for t in registry.for_caller(_caller(Role.OWNER, via="assistant"))] == ["t_admin_read"]

    def test_an_entry_its_host_does_not_offer_is_not_listed(self):
        registry.register(_tool(offered=lambda: False))
        assert registry.for_caller(_caller()) == []

    def test_the_paginated_schema_takes_a_cursor_and_a_limit(self):
        schema = _tool(paginated=True).schema()
        assert {"cursor", "limit"} <= set(schema["properties"])
        assert "cursor" not in _tool().schema()["properties"]

    def test_only_a_tool_that_asks_for_a_token_offers_one(self):
        assert "confirm_token" in _act(irreversible=True).schema(confirm_token=True)["properties"]
        assert "confirm_token" not in _act(irreversible=True).schema()["properties"]


class TestInvokeChecksBeforeTheHandler:
    async def test_an_unknown_name_is_refused(self):
        with pytest.raises(ToolRefusal, match="Unknown tool"):
            await invoke("nope", _caller(), {})

    async def test_a_role_too_low_is_refused_and_says_which(self):
        called = []

        async def handler(caller, args):
            called.append(1)
            return {}

        registry.register(_act(handler=handler))
        with pytest.raises(ToolRefusal, match="builder role.*viewer"):
            await invoke("t_act", _caller(Role.VIEWER), {})
        assert called == []

    async def test_a_scope_not_granted_is_refused_and_says_which(self):
        registry.register(_act())
        with pytest.raises(ToolRefusal, match="'run'"):
            await invoke("t_act", _caller(scopes=frozenset({"read"})), {})

    async def test_an_app_only_entry_is_refused_over_mcp_even_by_name(self):
        registry.register(_tool("t_admin_read", app_only=True, min_role=Role.ADMIN))
        with pytest.raises(ToolRefusal, match="Unknown tool"):
            await invoke("t_admin_read", _caller(Role.OWNER, via="mcp"), {})

    async def test_a_read_runs_and_is_never_audited(self, audited):
        registry.register(_tool())
        assert (await invoke("t_read", _caller(Role.VIEWER), {"id": "x"}))["ok"] is True
        assert audited == []


class TestEveryActIsAuditedOnce:
    async def test_the_event_names_the_person_the_surface_and_the_target(self, audited):
        registry.register(_act(target=lambda args, result: ("orchestration", args["id"])))
        await invoke("t_act", _caller(user_id="u9"), {"id": "orch_1"})
        assert len(audited) == 1
        e = audited[0]
        assert (e.action, e.tool, e.via, e.user_id) == ("test.t_act", "t_act", "mcp", "u9")
        assert (e.target_type, e.target_id) == ("orchestration", "orch_1")

    async def test_the_audit_runs_while_its_caller_is_still_bound(self):
        """A host's audit reads who acted from `caller.current()` (cloud's principal rides on it); the
        registry unbound the caller as the handler returned, so cloud wrote no row (found in the joint
        MCP2/ASSIST1 cloud suite)."""
        from core.product_tools import caller as caller_mod

        seen = []

        async def record(event):
            seen.append(caller_mod.current())

        hooks.set_audit(record)
        registry.register(_act())
        who = _caller(user_id="u7")
        await invoke("t_act", who, {})
        assert seen == [who]
        assert caller_mod.current() is None

    async def test_an_entry_may_name_its_action(self, audited):
        registry.register(_act(audit_action="run.started"))
        await invoke("t_act", _caller(), {})
        assert audited[0].action == "run.started"

    async def test_the_arguments_are_summarised_without_secrets(self, audited):
        registry.register(_act())
        await invoke("t_act", _caller(), {"id": "a", "api_key": "sk-1", "headers": {"Authorization": "Bearer x"},
                                          "body": "y" * 1000})
        args = audited[0].args
        assert args["api_key"] == "[withheld]"
        assert args["headers"]["Authorization"] == "[withheld]"
        assert len(args["body"]) < 300

    async def test_an_act_whose_handler_audits_itself_is_not_audited_twice(self, audited):
        registry.register(_act(self_audited=True))
        await invoke("t_act", _caller(), {})
        assert audited == []

    async def test_a_failed_act_is_not_audited(self, audited):
        async def boom(caller, args):
            raise ToolRefusal("no")

        registry.register(_act(handler=boom))
        with pytest.raises(ToolRefusal):
            await invoke("t_act", _caller(), {})
        assert audited == []

    async def test_an_audit_that_fails_does_not_undo_or_fail_the_act(self):
        async def broken(event):
            raise RuntimeError("audit down")

        hooks.set_audit(broken)
        registry.register(_act())
        assert (await invoke("t_act", _caller(), {}))["ok"] is True


class TestAConfirmedAct:
    async def test_an_irreversible_act_over_mcp_first_answers_with_a_preview_and_a_token(self, audited):
        done = []

        async def delete(caller, args):
            done.append(args["id"])
            return {"deleted": args["id"]}

        async def preview(caller, args):
            return {"will_delete": args["id"], "used_by": []}

        registry.register(_act("t_delete", irreversible=True, handler=delete, preview=preview))
        first = await invoke("t_delete", _caller(), {"id": "a"})
        assert first["status"] == "needs_confirmation"
        assert first["preview"] == {"will_delete": "a", "used_by": []}
        assert done == [] and audited == []

        second = await invoke("t_delete", _caller(), {"id": "a", "confirm_token": first["confirm_token"]})
        assert second == {"deleted": "a"}
        assert done == ["a"] and len(audited) == 1

    async def test_a_reversible_act_over_mcp_needs_no_token(self):
        registry.register(_act())
        assert (await invoke("t_act", _caller(), {}))["ok"] is True

    async def test_the_assistant_confirms_every_act(self):
        registry.register(_act())
        first = await invoke("t_act", _caller(via="assistant"), {}, confirm_policy="every_act")
        assert first["status"] == "needs_confirmation"
        assert (await invoke("t_act", _caller(via="assistant"), {"confirm_token": first["confirm_token"]},
                             confirm_policy="every_act"))["ok"] is True

    async def test_a_token_is_used_once(self):
        registry.register(_act(irreversible=True))
        token = (await invoke("t_act", _caller(), {"id": "a"}))["confirm_token"]
        await invoke("t_act", _caller(), {"id": "a", "confirm_token": token})
        with pytest.raises(ToolRefusal, match="already used"):
            await invoke("t_act", _caller(), {"id": "a", "confirm_token": token})

    async def test_a_token_is_for_these_arguments_only(self):
        registry.register(_act(irreversible=True))
        token = (await invoke("t_act", _caller(), {"id": "a"}))["confirm_token"]
        with pytest.raises(ToolRefusal, match="different"):
            await invoke("t_act", _caller(), {"id": "b", "confirm_token": token})

    async def test_a_token_is_for_this_person_only(self):
        registry.register(_act(irreversible=True))
        token = (await invoke("t_act", _caller(user_id="u1"), {"id": "a"}))["confirm_token"]
        with pytest.raises(ToolRefusal, match="someone else"):
            await invoke("t_act", _caller(user_id="u2"), {"id": "a", "confirm_token": token})

    async def test_a_token_is_for_this_tool_only(self):
        registry.register(_act("t_one", irreversible=True))
        registry.register(_act("t_two", irreversible=True))
        token = (await invoke("t_one", _caller(), {"id": "a"}))["confirm_token"]
        with pytest.raises(ToolRefusal, match="different"):
            await invoke("t_two", _caller(), {"id": "a", "confirm_token": token})

    async def test_a_token_past_its_time_is_refused(self, monkeypatch):
        registry.register(_act(irreversible=True))
        token = (await invoke("t_act", _caller(), {"id": "a"}))["confirm_token"]
        real = confirm._now
        monkeypatch.setattr(confirm, "_now", lambda: real() + confirm.TTL_SECONDS + 1)
        with pytest.raises(ToolRefusal, match="expired"):
            await invoke("t_act", _caller(), {"id": "a", "confirm_token": token})

    async def test_a_forged_token_is_refused(self):
        registry.register(_act(irreversible=True))
        token = (await invoke("t_act", _caller(), {"id": "a"}))["confirm_token"]
        body, sig = token.rsplit(".", 1)
        with pytest.raises(ToolRefusal, match="not valid"):
            await invoke("t_act", _caller(), {"id": "a", "confirm_token": body + "." + sig[::-1]})

    async def test_the_role_is_checked_again_when_the_token_is_redeemed(self):
        """A person demoted between the preview and the click cannot finish the act."""
        registry.register(_act(irreversible=True))
        token = (await invoke("t_act", _caller(Role.BUILDER), {"id": "a"}))["confirm_token"]
        with pytest.raises(ToolRefusal, match="builder role"):
            await invoke("t_act", _caller(Role.VIEWER), {"id": "a", "confirm_token": token})

    async def test_a_host_claim_store_decides_single_use(self):
        """Cloud's workers share one store (Redis SET NX); a claim it refuses is a used token."""
        class Refuses:
            async def claim(self, key, ttl):
                return False

        confirm.set_claim_store(Refuses())
        registry.register(_act(irreversible=True))
        token = (await invoke("t_act", _caller(), {"id": "a"}))["confirm_token"]
        with pytest.raises(ToolRefusal, match="already used"):
            await invoke("t_act", _caller(), {"id": "a", "confirm_token": token})


class TestAPage:
    def test_a_page_and_its_cursor(self):
        items = [{"n": i} for i in range(7)]
        one = page.page(items, {"limit": 3})
        assert [i["n"] for i in one["items"]] == [0, 1, 2]
        assert one["total"] == 7
        two = page.page(items, {"limit": 3, "cursor": one["next_cursor"]})
        assert [i["n"] for i in two["items"]] == [3, 4, 5]
        three = page.page(items, {"limit": 3, "cursor": two["next_cursor"]})
        assert [i["n"] for i in three["items"]] == [6]
        assert three["next_cursor"] is None

    def test_the_limit_has_a_ceiling(self):
        items = [{"n": i} for i in range(500)]
        assert len(page.page(items, {"limit": 10_000})["items"]) == page.MAX_LIMIT

    def test_a_page_too_big_is_cut_and_continues_where_it_was_cut(self):
        items = [{"text": "x" * 1000} for _ in range(10)]
        cut = page.page(items, {"limit": 10}, byte_cap=3500)
        assert 1 <= len(cut["items"]) < 10
        rest = page.page(items, {"limit": 10, "cursor": cut["next_cursor"]}, byte_cap=100_000)
        assert len(cut["items"]) + len(rest["items"]) == 10

    def test_one_item_bigger_than_the_cap_is_still_returned(self):
        assert len(page.page([{"text": "x" * 5000}], {}, byte_cap=100)["items"]) == 1

    def test_a_cursor_that_is_not_ours_is_refused(self):
        with pytest.raises(ToolRefusal, match="cursor"):
            page.page([], {"cursor": "garbage!"})


class TestRoles:
    def test_the_ladder(self):
        assert Role.OWNER.can_act_as(Role.ADMIN)
        assert Role.APPROVER.can_act_as(Role.VIEWER)
        assert not Role.APPROVER.can_act_as(Role.BUILDER)

    def test_a_role_parses_from_its_name(self):
        assert Role.parse("approver") is Role.APPROVER
        assert Role.parse(Role.ADMIN) is Role.ADMIN
        with pytest.raises(ValueError):
            Role.parse("member")
