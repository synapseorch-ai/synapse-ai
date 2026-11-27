"""A model this workspace cannot call is said at every door that sets one, and its call says why (F1762).

An agent was saved through the MCP connector on a platform model its host does not serve, and the save took it; its
first chat answered "The model provider refused it", as if a provider had, where the host's own gateway had said the
model is not enabled. The builder refuses such a model in its own run (F1250); the person's own doors (the app, the
API, MCP, the CLI) save what they name, because a key, a pulled model or a CLI's sign-in can come after. Now every
door that sets an agent's or a step's model says, beside what it saved, why that model cannot answer here yet, one
check for them all (`model_choices.save_check`); the agent's page asks when it opens; an import names it; and a chat
that fails on the model says the host's own sentence. An agent already on such a model loads and saves as it is.

A host names the providers whose list is its whole catalogue (`set_closed_providers`): a model of one that the list
does not offer is one the host does not serve, whatever the person sets up. Refusing that one at the person's doors is
one switch (`REFUSE_UNSERVED`), off until that is decided; its tests hold that branch with the switch on.
"""
from __future__ import annotations

import json

import pytest

from _fakes import seed as S
from core import builder_access, builder_tools  # noqa: F401 — the builder's modules, loaded as at boot
from core import model_choices
from core.native_builder import models_menu

UNSERVED = "synapse.claude-haiku-5-5"
SERVED = "synapse.claude-sonnet-4-6"
SAID = "Claude Haiku 5.5 (synapse.claude-haiku-5-5) isn't available on Synapse yet"
#: Each platform model's display name, as the host's catalogue gives it.
NAMES = {UNSERVED: "Claude Haiku 5.5", SERVED: "Claude Sonnet 4.6", "synapse.claude-haiku-4-5": "Claude Haiku 4.5"}


@pytest.fixture(autouse=True)
async def host():
    """A host whose platform catalogue is closed: what it lists is all it serves."""

    async def choices():
        return {"model_gateway": [SERVED, "synapse.claude-haiku-4-5"], "anthropic": ["claude-haiku-4-5"]}

    async def facts(ids):
        return {i: {"name": NAMES[i]} for i in ids if i in NAMES}

    model_choices.set_model_choices(choices)
    model_choices.set_closed_providers({"model_gateway": "Synapse"})
    models_menu.set_model_facts(facts)
    yield
    model_choices.set_model_choices(None)
    model_choices.set_closed_providers(None)
    models_menu.set_model_facts(None)


@pytest.fixture
def refusing(monkeypatch):
    """The branch still to be decided: a closed catalogue's unserved model refused at the person's doors."""
    monkeypatch.setattr(model_choices, "REFUSE_UNSERVED", True)


async def _mcp(tool: str, args: dict) -> dict:
    from core.product_tools import areas, registry
    from core.product_tools.caller import Caller
    from core.product_tools.roles import Role

    from core.product_tools.errors import ToolRefusal

    areas.ensure_loaded()
    try:
        out = await registry.invoke(tool, Caller(role=Role.BUILDER, via="mcp"), args)
    except ToolRefusal as refused:  # MCP answers a refusal as an error with its words
        return {"error": str(refused)}
    return json.loads(out) if isinstance(out, str) else out


def _agent(**over) -> dict:
    return {"id": over.pop("id", "agent_f1762"), "name": "Desk", "description": "Answers the desk.",
            "type": "conversational", "tools": [], "system_prompt": "Answer briefly.", **over}


class TestTheOneCheck:
    async def test_a_closed_catalogues_unserved_model_is_said_by_its_name_with_what_can_be_picked(self, client):
        refusal, note = await model_choices.save_check(UNSERVED)
        assert refusal is None
        assert note.startswith(SAID), note
        assert ("You can pick Claude Sonnet 4.6 (synapse.claude-sonnet-4-6), Claude Haiku 4.5 (synapse.claude-haiku-4-5),"
                " or leave the model out") in note, note

    async def test_one_offered_model_with_no_name_says_them_all_by_their_ids(self, client, caplog):
        """Never one form beside another: a catalogue row with no display name has every offered model said by its id,
        and the rows that have none are named in the log."""
        async def facts(ids):
            return {i: {"name": NAMES[i]} for i in ids if i in NAMES and i != "synapse.claude-haiku-4-5"}

        models_menu.set_model_facts(facts)
        _refusal, note = await model_choices.save_check(UNSERVED)
        assert "You can pick synapse.claude-sonnet-4-6, synapse.claude-haiku-4-5, or leave the model out" in note, note
        assert "has no display name" in caplog.text and "synapse.claude-haiku-4-5" in caplog.text

    async def test_a_model_it_serves_says_nothing(self, client):
        assert await model_choices.save_check(SERVED) == (None, None)

    async def test_no_model_says_nothing(self, client):
        assert await model_choices.save_check(None) == (None, None)
        assert await model_choices.save_check("  ") == (None, None)

    async def test_an_open_providers_model_with_no_key_is_noted_never_refused(self, client, refusing):
        refusal, note = await model_choices.save_check("gemini-2.0-flash-lite")
        assert refusal is None
        assert note.startswith("gemini-2.0-flash-lite can't answer here yet:") and "key is saved" in note, note

    async def test_a_list_that_cannot_be_read_refuses_and_says_nothing_of_the_catalogue(self, client, refusing):
        async def broken():
            raise RuntimeError("down")

        model_choices.set_model_choices(broken)
        assert await model_choices.save_check(UNSERVED) == (None, None)

    async def test_a_model_its_call_was_refused_for_says_that_sentence_without_the_gateways_lead(self, client):
        from core import model_availability

        await model_availability.mark("mistral-small-latest", "Model gateway refused the call: model 'mistral-small-latest' "
                                                            "does not exist or is not enabled.")
        _refusal, note = await model_choices.save_check("mistral-small-latest")
        assert note == ("mistral-small-latest can't answer here: model 'mistral-small-latest' does not exist or is not "
                        "enabled."), note

    async def test_the_switch_is_off_until_it_is_decided(self):
        assert model_choices.REFUSE_UNSERVED is False

    async def test_switched_on_it_refuses_a_new_unserved_model_and_never_one_the_agent_had(self, client, refusing):
        refusal, note = await model_choices.save_check(UNSERVED)
        assert refusal.startswith(SAID) and note is None
        refusal, note = await model_choices.save_check(UNSERVED, before=UNSERVED)
        assert refusal is None and note.startswith(SAID)


class TestTheAppAndTheApi:
    async def test_a_save_keeps_the_model_and_says_why_it_cannot_answer(self, client):
        r = await client.post("/api/agents", json=_agent(model=UNSERVED))
        assert r.status_code == 200, r.text
        assert r.json()["model"] == UNSERVED
        assert r.json()["model_note"].startswith(SAID)
        listed = next(a for a in (await client.get("/api/agents")).json() if a["id"] == "agent_f1762")
        assert listed["model_note"].startswith(SAID), "the list says it too: an agent saved by a script"

    async def test_a_model_it_serves_saves_with_no_note(self, client):
        r = await client.post("/api/agents", json=_agent(id="agent_f1762b", model=SERVED))
        assert r.status_code == 200 and r.json()["model_note"] is None

    async def test_the_page_asks_about_the_model_it_opened_with(self, client):
        r = await client.post("/api/agents/model-check", json={"model": UNSERVED})
        assert r.status_code == 200 and r.json()["model_note"].startswith(SAID)
        r = await client.post("/api/agents/model-check", json={"model": None})
        assert r.json()["model_note"] is None

    async def test_switched_on_a_new_one_is_refused_and_an_agent_already_on_it_saves(self, client, refusing):
        r = await client.post("/api/agents", json=_agent(id="agent_f1762c", model=UNSERVED))
        assert r.status_code == 422 and r.json()["detail"].startswith(SAID), r.text
        await S.seed_agents([S.make_agent(id="agent_f1762d", name="Old", model=UNSERVED)])
        r = await client.post("/api/agents", json=_agent(id="agent_f1762d", name="Old, renamed", model=UNSERVED))
        assert r.status_code == 200, r.text
        assert r.json()["model_note"].startswith(SAID)

    async def test_a_workflow_save_says_each_steps_model_it_cannot_call(self, client):
        orch = S.make_orchestration(id="orch_f1762", entry_step_id="s1", steps=[
            {"id": "s1", "type": "llm", "name": "Draft", "prompt_template": "Draft it.", "model": UNSERVED,
             "output_key": "draft", "next_step_id": None}])
        r = await client.post("/api/orchestrations", json=orch)
        assert r.status_code in (200, 201), r.text
        [note] = r.json()["model_notes"]
        assert note["step_id"] == "s1" and note["model"] == UNSERVED and note["note"].startswith(SAID)


class TestMcpAndTheCli:
    async def test_create_agent_saves_what_was_named_and_says_why(self, client):
        out = await _mcp("create_agent", {"name": "Mine", "type": "conversational", "tools": [], "model": UNSERVED})
        assert out["agent"]["model"] == UNSERVED
        assert out["model_note"].startswith(SAID)

    async def test_update_agent_says_it_for_the_model_it_sets(self, client):
        await S.seed_agents([S.make_agent(id="agent_f1762e", name="Desk two", model=SERVED)])
        out = await _mcp("update_agent", {"agent_id": "agent_f1762e", "fields": {"model": UNSERVED}})
        assert out["agent"]["model"] == UNSERVED and out["model_note"].startswith(SAID)

    async def test_create_agents_says_it_for_each_agent(self, client):
        out = await _mcp("create_agents", {"agents": [
            {"name": "One", "role": "one", "type": "conversational", "tools": [], "model": UNSERVED,
             "description": "One", "system_prompt": "Do one."},
            {"name": "Two", "role": "two", "type": "conversational", "tools": [], "model": SERVED,
             "description": "Two", "system_prompt": "Do two."}]})
        notes = {n["model"]: n["note"] for n in out["model_notes"]}
        assert list(notes) == [UNSERVED] and notes[UNSERVED].startswith(SAID), out

    async def test_a_step_door_says_it_for_the_steps_model(self, client):
        out = await _mcp("create_orchestration", {
            "name": "Drafts", "entry_step_id": "s1", "state_schema": {},
            "steps": [{"id": "s1", "type": "llm", "name": "Draft", "prompt_template": "Draft it.",
                       "model": UNSERVED, "output_key": "draft", "next_step_id": None}]})
        assert out["status"] == "created", out
        [note] = out["model_notes"]
        assert note["step_id"] == "s1" and note["note"].startswith(SAID)

    async def test_switched_on_the_doors_refuse_a_new_one_and_save_nothing(self, client, refusing):
        out = await _mcp("create_agent", {"name": "Refused", "type": "conversational", "tools": [],
                                          "model": UNSERVED})
        assert out["error"].startswith(SAID), out
        out = await _mcp("create_orchestration", {
            "name": "Refused drafts", "entry_step_id": "s1", "state_schema": {},
            "steps": [{"id": "s1", "type": "llm", "name": "Draft", "prompt_template": "Draft it.",
                       "model": UNSERVED, "output_key": "draft", "next_step_id": None}]})
        assert out["error"].startswith(f"Step Draft: {SAID}"), "named by its step"
        names = {a.get("name") for a in (await client.get("/api/agents")).json()}
        assert "Refused" not in names

    async def test_switched_on_a_step_that_already_had_it_saves(self, client, refusing):
        await S.seed_orchestrations([S.make_orchestration(id="orch_f1762b", entry_step_id="s1", steps=[
            {"id": "s1", "type": "llm", "name": "Draft", "prompt_template": "Draft it.", "model": UNSERVED,
             "output_key": "draft", "next_step_id": None}])])
        out = await _mcp("update_step", {"orch_id": "orch_f1762b", "step_id": "s1",
                                         "patch": {"name": "Draft it"}})
        assert "error" not in out, out


class TestAnImport:
    async def test_it_names_the_unserved_model_in_the_hosts_words(self, client):
        from core.routes import import_export

        warnings = await import_export._warnings(  # noqa: SLF001 — the preview's own reader
            [], [_agent(id="agent_f1762i", model=UNSERVED)], [])
        said = [w["message"] for w in warnings if w["id"] == "agent_f1762i"]
        assert any(SAID in m for m in said), warnings


class TestTheCallSaysTheHostsSentence:
    def test_a_gateway_refusal_is_said_in_the_gateways_words_not_as_a_providers(self):
        from core.llm_providers import LLMError
        from core.model_failures import GATEWAY_REFUSED
        from core.routes.api_v1 import _model_failure

        exc = LLMError(f"{GATEWAY_REFUSED}Claude Haiku 5.5 isn't available on Synapse yet.", provider="gateway",
                       status=404, code="provider_refused")
        said = _model_failure(exc.code, exc)
        assert "Claude Haiku 5.5 isn't available on Synapse yet." in said
        assert "The model provider refused it" not in said

    def test_a_providers_own_refusal_is_still_said_by_its_kind(self):
        from core.llm_providers import LLMError
        from core.routes.api_v1 import _model_failure

        exc = LLMError("Gemini said: the account behind this request is suspended", provider="gemini", status=403,
                       code="provider_refused")
        said = _model_failure(exc.code, exc)
        assert "The model provider refused it" in said and "suspended" not in said

    def test_a_fleet_turns_event_says_the_gateways_words_from_its_reason(self):
        from core.model_failures import GATEWAY_REFUSED
        from core.routes.api_v2 import _for_an_api_caller

        event = {"type": "error", "code": "provider_refused",
                 "message": f"LLMError: {GATEWAY_REFUSED}Claude Haiku 5.5 isn't available on Synapse yet."}
        said = _for_an_api_caller(event)["message"]
        assert "Claude Haiku 5.5 isn't available on Synapse yet." in said
