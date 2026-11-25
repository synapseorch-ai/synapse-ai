"""A Tool step whose model writes the call sends only what fits the tool and only addresses the run holds (F1743).

Without this, a send step's model that never sees the approved address ("To: marta@vellamar.example" on the Human
step's card) can write `to: []`, be refused by the relay, and on retry, told only the relay's rule, invent an address
that fits: what goes out is not what the person approved. So on every attempt, before the call, the arguments are
held to the tool's schema (a misfit goes back to the model and nothing is sent), and an email, a link's host or a
phone number the run does not hold stops the step on the approval card, naming it.
The run holds its state, what its Human steps showed (kept as values), and the step's own prompt; never the tool's
description, an earlier attempt's error, or what a person declined. A person who declines and writes the right
address gets one corrected call, shown to them before it goes out.

Near misses read as before: an address the run holds is sent, a link on a host it holds too, arguments set on the
step are not held here, a tool that only reads may be given any link, and a date or an order number is no phone.
"""
from __future__ import annotations

import types

import httpx
import pytest

from _fakes import seed as S
from _fakes.fake_llm import tool_call

def grounded():
    """The guard's module, read when a test needs it: the engine's own behaviour is what the rest hold."""
    from core.orchestration import grounded as module

    return module


MARTA = "marta@vellamar.example"
MADE_UP = "enquiry.contact@customer.example"
EMAIL_SCHEMA = {"type": "object", "required": ["to", "subject", "text"], "properties": {
    "to": {"type": "string", "description": "Who it goes to"},
    "subject": {"type": "string", "description": "The subject"},
    "text": {"type": "string", "description": "The email"}}}
DRAFT = {"recipient_email": MARTA, "subject": "Your coffee deliveries", "email_body": "Dear Marta, …",
         "page": "https://harborpine.example/offices"}


@pytest.fixture
def sent(monkeypatch):
    """Every request an HTTP tool sends, answered by `sent.outcome`: a status code, and `sent.body` its text."""
    import core.http_tool as http_tool

    record = types.SimpleNamespace(calls=[], outcome=None, body="{}")

    async def send(client, tool, args, variables, **kw):
        record.calls.append((tool["name"], args))
        return httpx.Response(record.outcome or 200, text=record.body, request=httpx.Request("POST", tool["url"]))

    monkeypatch.setattr(http_tool, "send", send)
    return record


async def _tool(name, method="POST", schema=None, description="Sends an email through our mail service."):
    from core.routes.tools import _upsert_custom_tools

    await _upsert_custom_tools([{"name": name, "description": description, "tool_type": "http", "method": method,
                                 "url": "https://relay.harborpine.example/send",
                                 "inputSchema": schema or EMAIL_SCHEMA}])


def _send_step(tool="send_email", **extra):
    return {"id": "send", "name": "Send email", "type": "tool", "forced_tool": tool,
            "prompt_template": "Send the approved reply.", "output_key": "sent", "model": "claude-x",
            "max_turns": 3, "next_step_id": None, **extra}


def _orch(*steps, orch_id="orch_f1743"):
    steps = [dict(s) for s in steps]
    for here, after in zip(steps, steps[1:]):
        here.setdefault("next_step_id", after["id"])
    return S.make_orchestration(id=orch_id, entry_step_id=steps[0]["id"], steps=steps)


CHECK = {"id": "check", "name": "Check the reply", "type": "human", "output_key": "check",
         "human_prompt": "To: {{state.claim_and_draft.recipient_email}}\nSubject: {{state.claim_and_draft.subject}}",
         "human_fields": [{"name": "approved", "type": "boolean", "label": "Send this reply?"}]}


async def _run(orch, run_id, state=None):
    from core.models_orchestration import Orchestration
    from core.orchestration.engine import OrchestrationEngine

    await S.seed_orchestrations([orch])
    server = types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})
    return [e async for e in OrchestrationEngine(Orchestration.model_validate(orch), server).run(
        "A new lead's card", run_id=run_id, initial_state={"claim_and_draft": DRAFT, **(state or {})})]


async def _resume(run_id, answer):
    from core.orchestration.engine import OrchestrationEngine

    server = types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})
    return [e async for e in OrchestrationEngine.resume(run_id, answer, server)]


def _asked(events) -> dict | None:
    return next((e for e in events if e.get("type") == "human_input_required"), None)


def _status(events) -> str | None:
    done = [e for e in events if e.get("type") == "orchestration_complete"]
    return done[-1].get("status") if done else None


async def _approved_then_send(fake_llm, run_id, *replies, step=None):
    """The run up to the send: the card shown and approved, then the send step's model replies `replies`."""
    await _tool("send_email")
    fake_llm.script(list(replies))
    asked = _asked(await _run(_orch(CHECK, step or _send_step()), run_id))
    assert asked and MARTA in asked["prompt"], asked
    return await _resume(run_id, {"approved": True})


def _email(to, **kw):
    return tool_call("send_email", to=to, subject=kw.get("subject", "Your coffee deliveries"),
                     text=kw.get("text", "Dear Marta, …"))


class TestTheFailureAsItHappened:
    async def test_an_empty_to_never_reaches_the_relay_and_a_made_up_one_stops_on_the_card(self, fake_llm, sent):
        events = await _approved_then_send(fake_llm, "r_f1743_a", tool_call("send_email", to=[], subject="s",
                                                                            text="t"), _email(MADE_UP))
        assert sent.calls == [], "neither the empty call nor the made-up address is sent"
        asked = _asked(events)
        assert asked and asked["kind"] == "tool_approval" and MADE_UP in asked["prompt"]
        assert "isn't anywhere in this run" in asked["prompt"] and grounded().CORRECT_LINE in asked["prompt"]
        assert asked["tool_args"]["to"] == MADE_UP
        model_saw = fake_llm.calls[-1].get("prompt_msg") or ""
        assert "'to' must be string, not an array" in model_saw, "the misfit went back to the model as its error"

    async def test_a_page_opened_later_shows_the_same_words(self, fake_llm, sent):
        from core.orchestration.state_store import StoreStateBackend

        events = await _approved_then_send(fake_llm, "r_f1743_a2", _email(MADE_UP))
        run = await StoreStateBackend().load("r_f1743_a2")
        assert run.waiting_for_human and run.human_prompt == _asked(events)["prompt"]

    async def test_declined_with_no_address_it_fails_and_nothing_is_sent(self, fake_llm, sent):
        await _approved_then_send(fake_llm, "r_f1743_b", _email(MADE_UP))
        events = await _resume("r_f1743_b", {"approve": False, "reason": "Not this one"})
        assert sent.calls == [] and _status(events) == "failed"
        assert "a person refused it: Not this one" in " ".join(str(e.get("response") or "") for e in events)

    async def test_approved_as_it_is_it_is_sent_once_exactly_as_shown(self, fake_llm, sent):
        await _approved_then_send(fake_llm, "r_f1743_c", _email(MADE_UP))
        await _resume("r_f1743_c", {"approve": True})
        assert [args["to"] for _, args in sent.calls] == [MADE_UP]


class TestACorrectedDecline:
    async def test_the_persons_address_is_shown_again_then_sent_once_on_a_yes(self, fake_llm, sent):
        await _approved_then_send(fake_llm, "r_f1743_d", _email(MADE_UP))
        fake_llm.script([_email(MARTA)])
        events = await _resume("r_f1743_d", {"approve": False, "reason": f"Send it to {MARTA}, the lead."})
        asked = _asked(events)
        assert sent.calls == [], "a corrected call is seen before it goes out, even when the run holds it"
        assert asked and asked["tool_args"]["to"] == MARTA and "written from what you said" in asked["prompt"]
        assert f"They said: Send it to {MARTA}" in (fake_llm.calls[-1].get("prompt_msg") or "")
        await _resume("r_f1743_d", {"approve": True})
        assert [args["to"] for _, args in sent.calls] == [MARTA]

    async def test_a_reason_that_repeats_the_made_up_address_sends_nothing_to_it(self, fake_llm, sent):
        await _approved_then_send(fake_llm, "r_f1743_e", _email(MADE_UP))
        fake_llm.script([_email(MADE_UP)])
        events = await _resume("r_f1743_e", {"approve": False, "reason": f"{MADE_UP} isn't a real address"})
        assert sent.calls == []
        asked = _asked(events)
        assert _status(events) == "failed" or (asked and MADE_UP in asked["prompt"]), "a failure or another pause"

    async def test_a_second_miss_pauses_again_and_never_loops_on_its_own(self, fake_llm, sent):
        await _approved_then_send(fake_llm, "r_f1743_f", _email(MADE_UP))
        fake_llm.script([_email("someone@else.example")])
        events = await _resume("r_f1743_f", {"approve": False, "reason": f"Use {MARTA}"})
        asked = _asked(events)
        assert sent.calls == [] and asked and "someone@else.example" in asked["prompt"]
        assert len([c for c in fake_llm.calls if "They said" in (c.get("prompt_msg") or "")]) == 1


class TestWhatTheRunHolds:
    async def test_an_address_the_run_holds_is_sent_whatever_its_case(self, fake_llm, sent):
        await _approved_then_send(fake_llm, "r_f1743_g", _email("Marta@Vellamar.example"))
        assert [args["to"] for _, args in sent.calls] == ["Marta@Vellamar.example"]

    async def test_a_link_on_a_host_the_run_holds_is_sent(self, fake_llm, sent):
        await _approved_then_send(fake_llm, "r_f1743_h", _email(
            MARTA, text="Our plans: https://harborpine.example/search?q=grinder"))
        assert len(sent.calls) == 1

    async def test_an_address_only_on_the_cards_it_showed_is_sent(self, fake_llm, sent):
        from core import variables

        await variables.save({"desk_email": "desk@harborpine.example"})
        check = {**CHECK, "human_prompt": CHECK["human_prompt"] + "\nReply-to: {{ global.desk_email }}"}
        await _tool("send_email")
        fake_llm.script([_email(MARTA, text="Write to desk@harborpine.example")])
        await _run(_orch(check, _send_step()), "r_f1743_i")
        await _resume("r_f1743_i", {"approved": True})
        assert len(sent.calls) == 1, "what a Human step's card showed is the run's to send"

    async def test_what_a_card_showed_is_kept_as_its_values_only(self, fake_llm, sent):
        from core.orchestration.state_store import StoreStateBackend

        await _approved_then_send(fake_llm, "r_f1743_j", _email(MARTA))
        run = await StoreStateBackend().load("r_f1743_j")
        assert run.shared_state[grounded().SHOWN_KEY] == {"check": [MARTA, "vellamar.example"]}

    async def test_a_send_after_a_loop_of_approvals_holds_every_card(self, fake_llm, sent):
        """Three recipients approved one card each, the send after the loop mailing all three: the loop's item is
        replaced each time and a step after the loop writes over the list, so the first two are only on the cards
        they showed."""
        leads = ["ana@lisbon.example", "ben@porto.example", "cai@faro.example"]
        loop = {"id": "each", "name": "Each lead", "type": "loop", "loop_over": "state.leads",
                "loop_step_ids": ["ok"], "next_step_id": "tidy"}
        ok = {"id": "ok", "name": "Approve the lead", "type": "human", "output_key": "ok",
              "human_prompt": "To: {{state.loop_item}}", "next_step_id": None,
              "human_fields": [{"name": "approved", "type": "boolean", "label": "Send to this lead?"}]}
        tidy = {"id": "tidy", "name": "Tidy", "type": "print", "print_content": "approved", "output_key": "leads",
                "next_step_id": "send"}
        await _tool("send_email")
        fake_llm.script([_email(", ".join(leads))])
        orch = S.make_orchestration(id="orch_f1743_loop", entry_step_id="each", steps=[loop, ok, tidy, _send_step()])
        asked = _asked(await _run(orch, "r_f1743_loop", state={"leads": leads}))
        for lead in leads:
            assert asked and lead in asked["prompt"], asked
            events = await _resume("r_f1743_loop", {"approved": True})
            asked = _asked(events)
        assert len(sent.calls) == 1 and not asked, "every address in the send was shown to a person in this run"

    def test_the_shown_values_are_a_union_kept_to_a_cap(self):
        g = grounded()
        state: dict = {}
        for lead in ("ana@lisbon.example", "ben@porto.example"):
            g.record_shown(state, "ok", f"To: {lead}", [])
        assert {"ana@lisbon.example", "ben@porto.example"} <= g.held(state, "")
        many = " ".join(f"p{i}@many.example" for i in range(g.SHOWN_CAP + 20))
        g.record_shown(state, "bulk", many, [])
        kept = [k for v in state[g.SHOWN_KEY].values() for k in v]
        assert len(kept) == g.SHOWN_CAP and "ana@lisbon.example" in kept, "the first values are kept"

    async def test_an_address_only_in_the_tools_description_stops(self, fake_llm, sent):
        await _tool("send_email", description="Sends an email, e.g. to help@harborpine.example")
        fake_llm.script([_email("help@harborpine.example")])
        events = await _run(_orch(_send_step()), "r_f1743_k")
        assert sent.calls == [] and "help@harborpine.example" in (_asked(events) or {}).get("prompt", "")

    async def test_an_address_only_in_an_earlier_attempts_error_stops(self, fake_llm, sent):
        sent.outcome, sent.body = 422, "unknown mailbox; did you mean billing@vellamar.example?"
        await _approved_then_send(fake_llm, "r_f1743_l", _email(MARTA), _email("billing@vellamar.example"))
        assert [args["to"] for _, args in sent.calls] == [MARTA], "the error's address is never sent"

    async def test_arguments_set_on_the_step_are_not_held_here(self, fake_llm, sent):
        step = _send_step(tool_arguments={"to": "ops@harborpine.example", "subject": "s", "text": "t"})
        await _tool("send_email")
        events = await _run(_orch(step), "r_f1743_m")
        assert [args["to"] for _, args in sent.calls] == ["ops@harborpine.example"] and not _asked(events)


class TestLinksAndNumbers:
    URL_SCHEMA = {"type": "object", "required": ["url"], "properties": {"url": {"type": "string"}}}

    async def test_a_tool_that_only_reads_may_be_given_any_link(self, fake_llm, sent):
        await _tool("search_news", method="GET", schema=self.URL_SCHEMA, description="Reads a page")
        fake_llm.script([tool_call("search_news", url="https://www.bbc.co.uk/search?q=coffee")])
        events = await _run(_orch(_send_step("search_news")), "r_f1743_n")
        assert len(sent.calls) == 1 and not _asked(events)

    async def test_the_same_link_to_a_tool_that_writes_stops(self, fake_llm, sent):
        await _tool("post_link", method="POST", schema=self.URL_SCHEMA, description="Posts a link")
        fake_llm.script([tool_call("post_link", url="https://www.bbc.co.uk/search?q=coffee")])
        events = await _run(_orch(_send_step("post_link")), "r_f1743_o")
        assert sent.calls == [] and "https://www.bbc.co.uk/search?q=coffee" in _asked(events)["prompt"]

    async def test_a_phone_number_the_run_does_not_hold_stops(self, fake_llm, sent):
        schema = {"type": "object", "required": ["phone", "text"], "properties": {
            "phone": {"type": "string", "description": "Mobile number"}, "text": {"type": "string"}}}
        await _tool("send_sms", schema=schema, description="Sends a text message")
        fake_llm.script([tool_call("send_sms", phone="+351 912 345 678", text="Hi")])
        events = await _run(_orch(_send_step("send_sms")), "r_f1743_p")
        assert sent.calls == [] and "+351 912 345 678" in _asked(events)["prompt"]

    def test_a_date_or_an_order_number_is_no_phone(self):
        schema = {"type": "object", "properties": {"date": {"type": "string"}, "order": {"type": "string"}}}
        assert grounded().values_in({"date": "2026-11-25", "order": "4500012345"}, schema) == []
        assert [v.kind for v in grounded().values_in({"mobile": "912 345 678"}, {})] == ["phone"]


class TestTheSchemaCheck:
    def test_a_required_text_left_blank_is_a_misfit(self):
        said = grounded().misfits("send_email", EMAIL_SCHEMA, {"to": " ", "subject": "s", "text": "t"})
        assert said == ["send_email's input 'to' is required and cannot be blank"]

    def test_a_required_list_left_empty_is_not(self):
        """An agent made with no tools is `"tools": []`: an empty list or object a tool requires is still a value."""
        schema = {"type": "object", "required": ["name", "tools"],
                  "properties": {"name": {"type": "string"}, "tools": {"type": "array", "items": {"type": "string"}}}}
        assert grounded().misfits("create_agent", schema, {"name": "R", "tools": []}) == []

    def test_one_left_out_and_one_of_another_type(self):
        said = grounded().misfits("send_email", EMAIL_SCHEMA, {"to": [], "subject": "s"})
        assert "send_email needs 'text'" in said and "send_email's input 'to' must be string, not an array" in said

    def test_a_schema_it_cannot_read_holds_nothing(self):
        assert grounded().misfits("t", {"type": "objekt"}, {"to": []}) == []
        assert grounded().misfits("t", {"$ref": "#/nowhere"}, {"to": []}) == []

    async def test_a_blank_required_input_is_retried_within_the_attempts(self, fake_llm, sent):
        await _approved_then_send(fake_llm, "r_f1743_q", _email(""), _email(MARTA))
        assert [args["to"] for _, args in sent.calls] == [MARTA]
