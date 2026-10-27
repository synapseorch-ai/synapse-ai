"""The answer route's context is built by code, from what the answer needs (SAI2-P4, F980; P4-PROMPT §3 and §7).

`step_answer` (an E1 step: no model) builds the pack; then one of three answer steps writes the reply:
* **none** (talk, ideas, thanks, "what can you do"): no tools at all. The pack still carries the workspace's name
  and description, the asker, and what the product can do, so the reply never guesses the business (rc4.2 on prod:
  "hi" called Kestrel, a freight company, "your manufacturing business") and never invents a feature.
* **help:** the product's help hits and its capabilities, found in code (no quick call).
* **workspace:** the acts and changes this asker's role may ask for (only those: the reporter never promises what
  the role can't do), and the things the message names. A builder's pack lists deleting an agent, so the reply
  never says that tool is missing (F958's class).

Connect cards come from code, only for a provider the person named and the workspace can connect, once per
conversation unless asked again (SAI2's review, after Naveen's Firecrawl case on prod), and one card per turn.
"""
from __future__ import annotations

import pytest

from core.native_builder.answer_pack import build_pack, describe_workspace
from core.product_tools.roles import Role

ROUTED = {"kind": "answer", "needs": "none", "route": "step_answer", "providers": []}


def _pack(needs="none", role=Role.OWNER, **kw):
    base = dict(routed={**ROUTED, "needs": needs}, workspace_name="Kestrel", profile="", asker_name="Ana",
                asker_role=role, last="", capabilities="What this product does:\n- Workflows.", help_hits=[],
                named=[])
    return build_pack(**{**base, **kw})


class TestEveryPackSaysWhoAndWhere:
    def test_the_workspace_and_the_asker(self):
        pack = _pack(profile="a logistics and freight business of about 2-10")
        assert "Kestrel" in pack and "logistics and freight" in pack
        assert "Ana" in pack and "owner" in pack

    @pytest.mark.parametrize("profile", ["", "(not recorded — ask the user if the answer would change what you build)"])
    def test_with_no_description_it_says_so_and_names_no_business(self, profile):
        pack = _pack(profile=profile)
        assert "no description" in pack.lower()
        assert "say nothing about what the business does" in pack.lower()
        assert "manufactur" not in pack.lower()

    def test_an_unknown_name_is_left_out(self):
        """SAI2's review: OSS writes no workspace name; the pack never says "unnamed"."""
        pack = _pack(workspace_name="", asker_name="")
        assert "unnamed" not in pack and "Who is asking: a owner" not in pack
        assert "Who is asking: an owner in this workspace." in pack

    @pytest.mark.parametrize("needs", ["none", "help", "workspace"])
    def test_every_pack_says_what_this_person_may_ask_for(self, needs):
        """SAI2's review (10-27): "Is there a way to send invoices automatically" is talk, and a reply that only
        describes ends with no offer. Code tells every answer what the asker may ask for, in one line."""
        builder = _pack(needs, role=Role.BUILDER)
        assert "may ask you to build or change things here: offer it when they want something done" in builder
        viewer = _pack(needs, role=Role.VIEWER)
        assert "offer it when they want something done" not in viewer
        assert "may read and ask about this workspace, not build or change it" in viewer
        approver = _pack(needs, role=Role.APPROVER)
        assert "may answer what waits for them" in approver and "offer it when" not in approver

    def test_talk_gets_what_the_product_can_do_from_code(self):
        assert "What this product does" in _pack("none")

    def test_what_the_last_turn_did(self):
        assert "Turned off Nightly invoices." in _pack(last="Turned off Nightly invoices.")


class TestEveryPackSaysHowToReply:
    """F986 (lang-03, SAI2's re-read): a Spanish ask whose own words read like the builder's ("¿Quieres que te
    muestre…?") got an English reply about "the user". Classify already wrote the language; code now puts it in every
    pack as a fact, with who the reply is written to."""

    @pytest.mark.parametrize("code,name", [("es", "Spanish"), ("hi", "Hindi"), ("en", "English"), ("ES", "Spanish"),
                                           ("pt-BR", "Portuguese")])
    @pytest.mark.parametrize("needs", ["none", "help", "workspace"])
    def test_the_language_classify_read_is_named(self, code, name, needs):
        pack = _pack(needs, routed={**ROUTED, "needs": needs, "language": code})
        assert f"Reply in {name}, the language this message is written in" in pack

    @pytest.mark.parametrize("code", [None, "", "xx"])
    def test_an_unknown_language_is_still_the_messages_own(self, code):
        pack = _pack(routed={**ROUTED, "language": code})
        assert "Reply in the language this message is written in" in pack
        assert "Reply in English" not in pack

    @pytest.mark.parametrize("needs", ["none", "help", "workspace"])
    def test_the_reply_is_written_to_the_person(self, needs):
        pack = _pack(needs)
        assert 'Write to the person who asked, as "you"' in pack and '"the user"' in pack


class TestHelp:
    def test_help_hits_are_in_the_pack(self):
        hits = [{"title": "Call the API", "summary": "Use an API key.", "steps": ["Open Settings → API keys"],
                 "where": "Settings"}]
        pack = _pack("help", help_hits=hits)
        assert "Call the API" in pack and "Open Settings → API keys" in pack

    def test_no_hits_is_said(self):
        assert "the help says nothing" in _pack("help").lower()


class TestWhatTheRoleMayAsk:
    def test_a_builders_pack_lists_deleting_an_agent(self):
        pack = _pack("workspace", role=Role.BUILDER)
        assert "Delete an agent" in pack and "Turn a schedule off" in pack

    def test_a_viewers_pack_holds_no_act_and_no_write(self):
        pack = _pack("workspace", role=Role.VIEWER)
        assert "Delete an agent" not in pack and "Turn a schedule off" not in pack
        assert "viewer" in pack and "read" in pack.lower()

    def test_an_approvers_pack_holds_only_what_an_approver_may(self):
        pack = _pack("workspace", role=Role.APPROVER)
        assert "Delete an agent" not in pack
        assert "Answer the question a waiting run asks" in pack

    def test_only_a_workspace_answer_lists_them(self):
        assert "Delete an agent" not in _pack("none", role=Role.BUILDER)

    def test_named_things_are_in_the_pack_with_their_ids(self):
        pack = _pack("workspace", named=[{"kind": "schedule", "id": "sch_1", "name": "Nightly invoices",
                                          "detail": "0 2 * * *"}])
        assert '"Nightly invoices" (sch_1)' in pack and "0 2 * * *" in pack


class TestTheWorkspaceDescription:
    def test_the_industry_label_never_the_key(self):
        """F981 (Naveen 10-24): the label, the name and their own words — never a bare key that misreads."""
        assert describe_workspace("Kestrel", "a manufacturing or logistics business") == (
            "Kestrel: a manufacturing or logistics business")
        assert describe_workspace("", "") == ""


class TestTheStep:
    """`answer_pack` as `step_answer` calls it, and the answer route's two card rules at the tool door."""

    @pytest.fixture(autouse=True)
    async def _ready(self):
        from core import builder_access, builder_connect
        from core.product_tools import confirm
        from core.store import get_store

        async def options():
            return {"catalog": [{"slug": "firecrawl", "name": "Firecrawl", "auth": "bearer"},
                                {"slug": "notion", "name": "Notion", "auth": "oauth"}]}

        await get_store()
        confirm.set_claim_store(None)
        builder_access.forget_runs()
        builder_connect.set_connect_options(options)
        yield
        builder_connect.set_connect_options(None)
        builder_access.forget_runs()

    async def _run(self, run_id, message, *, session="builder:t:panel:ans", needs="none", providers=()):
        from core import builder_access
        from core.models_orchestration import OrchestrationRun
        from core.orchestration.state import SharedState

        run = OrchestrationRun(run_id=run_id, orchestration_id="orch_native_builder", session_id=session,
                               shared_state={builder_access.CALLER_KEY: {"user_id": None}, "user_message": message,
                                             "workspace_name": "Kestrel", "org_profile": "",
                                             "request": {"kind": "answer", "needs": needs, "route": "step_answer",
                                                         "providers": list(providers)}})
        await SharedState(run).checkpoint()
        return run

    async def _call(self, run, tool, args=None):
        import json

        from core.builder_tools import execute_builder_tool_with_events
        from core.orchestration.engine import live

        class _Server:
            agent_sessions: dict = {}
            tool_router: dict = {}

        with live(run):
            raw, events = await execute_builder_tool_with_events(tool, args or {}, _Server(), run_id=run.run_id)
        return json.loads(raw), events

    async def test_a_greeting_gets_the_workspace_and_no_card(self):
        run = await self._run("builder_hi", "hi")
        out, events = await self._call(run, "answer_pack")
        assert out["needs"] == "none" and "Kestrel" in out["pack"] and "no description" in out["pack"]
        assert events == []

    async def test_scraping_a_site_offers_no_service_the_person_did_not_name(self):
        run = await self._run("builder_scrape", "can you scrape example.com for me?", needs="help")
        out, events = await self._call(run, "answer_pack")
        assert events == [] and "Connect card" not in out["pack"]
        said, events = await self._call(run, "prepare_connection", {"kind": "app", "catalog_slug": "firecrawl",
                                                                     "title": "Connect Firecrawl to scrape"})
        assert events == [] and "did not name" in said["error"]

    async def test_a_named_service_gets_its_card_once_per_conversation(self):
        first = await self._run("builder_fc1", "connect Firecrawl", providers=["Firecrawl"])
        out, events = await self._call(first, "answer_pack")
        assert [e["app"]["slug"] for e in events if e.get("type") == "connection_needed"] == ["firecrawl"]
        assert "Connect card for Firecrawl is up" in out["pack"]

        again = await self._run("builder_fc2", "does Firecrawl cost money?", providers=["Firecrawl"])
        out, events = await self._call(again, "answer_pack")
        assert events == [] and "shown earlier" in out["pack"]

        asked = await self._run("builder_fc3", "connect Firecrawl again please", providers=["Firecrawl"])
        _, events = await self._call(asked, "answer_pack")
        assert [e.get("type") for e in events] == ["connection_needed"]

    async def test_a_card_shown_earlier_is_not_put_up_again_by_the_model(self):
        """SAI2's review 6: "never re-offered" holds for the answering model's own call too, across this conversation's
        earlier turns, unless the person asks for it again."""
        first = await self._run("builder_re1", "connect Firecrawl", session="builder:t:panel:re",
                                providers=["Firecrawl"])
        _, events = await self._call(first, "answer_pack")
        assert [e.get("type") for e in events] == ["connection_needed"]

        later = await self._run("builder_re2", "how much does Firecrawl cost?", session="builder:t:panel:re",
                                needs="help")
        said, events = await self._call(later, "prepare_connection", {"kind": "app", "catalog_slug": "firecrawl"})
        assert events == [] and "shown earlier" in said["error"]

        again = await self._run("builder_re3", "please connect Firecrawl again", session="builder:t:panel:re",
                                needs="help")
        _, events = await self._call(again, "prepare_connection", {"kind": "app", "catalog_slug": "firecrawl"})
        assert [e.get("type") for e in events] == ["connection_needed"]

        elsewhere = await self._run("builder_re4", "tell me about Firecrawl", session="builder:t:panel:other",
                                    needs="help")
        _, events = await self._call(elsewhere, "prepare_connection", {"kind": "app", "catalog_slug": "firecrawl"})
        assert [e.get("type") for e in events] == ["connection_needed"], "another conversation never saw it"

    async def test_one_card_per_turn(self):
        run = await self._run("builder_two", "connect Notion and Firecrawl", providers=["Notion", "Firecrawl"])
        _, events = await self._call(run, "answer_pack")
        assert len([e for e in events if e.get("type") == "connection_needed"]) == 1
        said, events = await self._call(run, "prepare_connection", {"kind": "app", "catalog_slug": "firecrawl"})
        assert events == [] and "One card per turn" in said["error"]

    async def test_outside_the_answer_route_the_rules_do_not_apply(self):
        run = await self._run("builder_build", "build me a scraper")
        run.shared_state["request"] = {"kind": "build", "route": "step_plan_room"}
        _, events = await self._call(run, "prepare_connection", {"kind": "app", "catalog_slug": "firecrawl"})
        assert [e.get("type") for e in events] == ["connection_needed"]


class TestTheAnswerSteps:
    """What each answer step is offered (F983) and the reporter's one read tool per job (F756, F982)."""

    def _bundle(self):
        from core.native_builder import bundled_definitions

        bundle = bundled_definitions()
        return {s["id"]: s for s in bundle["orchestration"]["steps"]}, {a["id"]: a for a in bundle["agents"]}

    def test_talk_is_offered_no_tool_at_all(self):
        """An agent step (so a picture reaches it) whose agent holds no tool and whose step allows none."""
        steps, agents = self._bundle()
        talk = steps["step_answer_talk"]
        assert talk["type"] == "agent" and talk["allowed_tools"] == [] and not talk.get("forced_tool")
        assert agents[talk["agent_id"]]["tools"] == [] and agents[talk["agent_id"]]["skip_default_tools"] is True
        assert "{state.answer_pack.pack}" in talk["prompt_template"]

    def test_help_is_offered_the_help_and_no_workspace_read(self):
        steps, _ = self._bundle()
        assert set(steps["step_answer_help"]["allowed_tools"]) == {"search_help", "product_capabilities",
                                                                   "prepare_connection"}

    def test_only_the_workspace_answer_looks_before_it_answers(self):
        steps, agents = self._bundle()
        assert "Look before you answer" in steps["step_answer_workspace"]["prompt_template"]
        assert "read_workspace" in steps["step_answer_workspace"]["allowed_tools"]
        assert "Look before you answer" not in agents["agent_native_builder_reporter"]["system_prompt"]
        assert "Look before you answer" not in steps["step_answer_help"]["prompt_template"]

    def test_the_reporter_holds_one_read_tool_per_job(self):
        """F756: runs, agents, workflows and schedules are `read_workspace` topics; the older tools for the same jobs
        stay registered for MCP, never offered to the reporter."""
        _, agents = self._bundle()
        tools = set(agents["agent_native_builder_reporter"]["tools"])
        assert "read_workspace" in tools
        assert not tools & {"list_agents", "get_agent", "list_orchestrations", "get_orchestration", "list_schedules",
                            "list_recent_runs", "get_run_summary"}

    def test_the_old_tools_are_still_served(self):
        from core.builder_tools import BUILDER_TOOL_NAMES

        assert {"list_agents", "list_orchestrations", "list_schedules", "list_recent_runs"} <= set(BUILDER_TOOL_NAMES)
