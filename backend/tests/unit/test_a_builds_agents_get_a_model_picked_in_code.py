"""
A build's new agents each get a model, picked in code from what this workspace can call (F966, SAI2-P3, D300; on
SAI2-P2's `models_menu`).

Every new agent got the workspace's default model. Now the planner says what each agent needs (`simple`,
`reasoning`, `vision`), the person's own wish when they named one (`model_wish`), and whether a stronger model would
clearly help (`stronger_helps`); it never writes an id. `check_build` picks:
- a pick → the model on the plan, with code's reason, on the card ("Writer — model: X (why)");
- a wish that could mean more than one model → a question, with the menu's names;
- nothing here can do the job → said, and Build it stays off;
- a stronger model that helps → offered at its price (`stronger:<agent>`), chosen only by the person's press.
The picks are in the plan, so the yes covers them; compile writes them; the save picks again and stops when the
menu moved since the yes; CE1 prices each agent step on its pick.
"""
from __future__ import annotations

import json

import pytest

from core import builder_access, builder_tools
from core.builder_tools import execute_builder_tool_with_events
from core.models_orchestration import OrchestrationRun
from core.native_builder import build_plan, models_menu as MM, objects as O
from core.orchestration.engine import live
from core.orchestration.state import SharedState
from core.product_tools import confirm, hooks
from core.product_tools.roles import Role


def _entry(id, *, tools="native", vision=False, reasoning=False, out=1.0, source="managed", pay=True):
    return MM.ModelEntry(id=id, name=id, provider=id.split(".")[1].split("-")[0], source=source, tools=tools,
                         vision=vision, reasoning=reasoning, price_in=out / 4 if out is not None else None,
                         price_out=out, can_pay=pay)


MENU = [_entry("synapse.text-only", tools="none", out=0.1), _entry("synapse.nova-lite", out=0.24),
        _entry("synapse.gpt-mini", out=2), _entry("synapse.claude-haiku", reasoning=True, out=5),
        _entry("synapse.claude-sonnet", reasoning=True, vision=True, out=15),
        _entry("synapse.claude-opus", reasoning=True, vision=True, out=75)]


WEATHER = {"name": "rotterdam_weather", "description": "Today's weather in Rotterdam.", "tool_type": "http",
           "method": "GET", "url": "https://api.weather.example/v1/rotterdam", "headers": {},
           "inputSchema": {"type": "object", "properties": {}}, "annotations": {"readOnlyHint": True}}


class _Server:
    agent_sessions: dict = {}
    tool_router: dict = {}


@pytest.fixture(autouse=True)
async def workspace(monkeypatch):
    confirm.set_claim_store(None)
    builder_access.forget_runs()
    menu = {"now": list(MENU)}

    async def fake_menu():
        return list(menu["now"])

    async def owner(user_id):
        return Role.OWNER

    from core.store.resources import save_tool

    monkeypatch.setattr(MM, "menu", fake_menu)
    hooks.set_role_source(owner)
    await save_tool(dict(WEATHER))
    yield menu
    hooks.set_role_source(None)
    builder_access.forget_runs()
    builder_tools.set_room_check(None)


TELL = {"id": "tell", "type": "agent", "name": "Draft the warning", "does": "Drafts a delay warning",
        "agent": "Warning writer", "prompt": "Draft a short delay warning."}


def _writer(**more):
    return {"name": "Warning writer", "purpose": "Writes short delay warnings for carriers", "tools": [], **more}


def _draft(*agents, steps=(TELL,)):
    return {"finish": "submit_plan", "kind": "create", "title": "Delay warning", "summary": "Warns carriers.",
            "steps": list(steps), "agents": list(agents)}


async def _call(tool, args, run):
    with live(run):
        raw, events = await execute_builder_tool_with_events(tool, args, _Server(), run_id=run.run_id)
    return (json.loads(raw) if isinstance(raw, str) else raw), events


async def _check(draft, run=None):
    """check_build as the route runs it: the prompt writer first when the plan makes agents (Q2)."""
    if run is None:
        run = OrchestrationRun(run_id="builder_models", orchestration_id="orch_native_builder",
                               shared_state={builder_access.CALLER_KEY: {"user_id": "u1"}, "intent": "create"})
        await SharedState(run).checkpoint()
    run.shared_state[build_plan.DRAFT_KEY] = draft
    checked, _ = await _call("check_build", {"draft": draft}, run)
    run.shared_state[build_plan.CHECKED_KEY] = checked
    if checked["outcome"] == "prompts":
        out, _ = await _call("submit_prompts", {"prompts": [{"agent": a["name"], "instructions": "Write it."}
                                                            for a in checked["plan"]["agents"]]}, run)
        run.shared_state[build_plan.PROMPTS_KEY] = out
        checked, _ = await _call("check_build", {"draft": draft}, run)
        run.shared_state[build_plan.CHECKED_KEY] = checked
    return run, checked


def _pick(checked, agent="Warning writer"):
    return (checked["plan"].get("models") or {}).get(agent)


# ── the pick ──────────────────────────────────────────────────────────────────────────────────────────────────────

async def test_a_simple_agent_gets_the_lowest_cost_model_that_fits_with_why_on_the_card():
    _, checked = await _check(_draft(_writer()))
    assert checked["outcome"] == "review", checked
    assert _pick(checked)["model"] == "synapse.text-only"
    assert "- **Warning writer**" in checked["review"]
    # One step, simple, no tools: a narrow job, the one the cheapest fit is for (F1251, replacing F966's rule).
    assert "model: synapse.text-only (the lowest-cost model that fits a narrow job, from the platform's models)" \
        in checked["review"]


async def test_an_agent_with_tools_never_gets_a_model_that_calls_none():
    _, checked = await _check(_draft(_writer(tools=["rotterdam_weather"])))
    assert _pick(checked)["model"] == "synapse.nova-lite"


async def test_reasoning_gets_the_strongest_within_the_ceiling_never_above_it():
    _, checked = await _check(_draft(_writer(needs="reasoning", why="careful legal wording")))
    assert _pick(checked)["model"] == "synapse.claude-sonnet"
    assert ("model: synapse.claude-sonnet (a stronger model, for reasoning, from the platform's models: one that "
            "reasons carefully; careful legal wording)") in checked["review"]


async def test_the_planner_never_writes_a_model_id():
    _, checked = await _check(_draft(_writer(model="synapse.claude-opus", models={"Warning writer": "x"})))
    assert _pick(checked)["model"] == "synapse.text-only"
    plan, _ = O.read({k: v for k, v in _draft(_writer(model="synapse.claude-opus")).items() if k != "finish"})
    assert plan.models == {} and "model" not in plan.agents[0].model_dump()


async def test_a_needs_that_is_not_one_of_the_three_goes_back_to_the_planner():
    plan, errors = O.read({k: v for k, v in _draft(_writer(needs="genius")).items() if k != "finish"})
    assert plan is None and any("needs" in e for e in errors)


# ── the person's own wish ─────────────────────────────────────────────────────────────────────────────────────────

async def test_a_family_the_person_named_is_theirs():
    _, checked = await _check(_draft(_writer(model_wish="Claude")))
    assert _pick(checked)["model"] == "synapse.claude-haiku"
    assert "you asked for Claude" in checked["review"]


async def test_a_model_named_outright_is_theirs_even_above_the_ceiling():
    _, checked = await _check(_draft(_writer(model_wish="claude opus")))
    assert _pick(checked)["model"] == "synapse.claude-opus"


async def test_a_wish_that_could_mean_two_is_asked_with_the_menus_names():
    _, checked = await _check(_draft(_writer(model_wish="Claude or GPT")))
    assert checked["outcome"] == "ask"
    assert "Warning writer" in checked["question"] and "synapse.gpt-mini" in checked["question"]


# ── nothing here can do it ────────────────────────────────────────────────────────────────────────────────────────

async def test_no_model_that_does_the_job_is_said_and_build_it_stays_off(workspace):
    workspace["now"] = [_entry("synapse.text-only", tools="none", out=0.1)]
    _, checked = await _check(_draft(_writer(tools=["rotterdam_weather"])))
    assert checked["outcome"] == "review" and "build" not in checked["offers"]
    assert "Warning writer: No model this workspace can use calls tools." in checked["issues"]
    assert "## Can't be saved yet" in checked["review"]


async def test_no_menu_leaves_the_workspace_default_as_before(workspace):
    workspace["now"] = []
    _, checked = await _check(_draft(_writer()))
    assert checked["outcome"] == "review" and "build" in checked["offers"]
    assert not checked["plan"].get("models")


# ── a stronger model, offered ─────────────────────────────────────────────────────────────────────────────────────

async def test_a_stronger_model_is_offered_at_its_price_only_where_it_helps():
    _, plain = await _check(_draft(_writer()))
    assert not [o for o in plain["offers"] if o.startswith("stronger:")]
    _, helps = await _check(_draft(_writer(stronger_helps=True)))
    assert "stronger:Warning writer" in helps["offers"]
    assert _pick(helps)["stronger"] == "synapse.claude-sonnet"
    assert _pick(helps)["stronger_price"] == "about $15 per 1M output tokens"


async def test_the_persons_press_picks_the_stronger_model_and_the_card_says_so():
    run, checked = await _check(_draft(_writer(stronger_helps=True)))
    run.shared_state["review_feedback"] = {"feedback": "Use a stronger model for Warning writer"}
    answer, _ = await _call("read_plan_answer", {}, run)
    assert answer == {"outcome": "stronger", "stronger": ["Warning writer"]}
    run.shared_state[build_plan.ANSWER_KEY] = answer
    _, again = await _check(_draft(_writer(stronger_helps=True)), run)
    assert _pick(again)["model"] == "synapse.claude-sonnet" and not _pick(again).get("stronger")
    assert "the stronger model you chose" in again["review"]
    assert again["digest"] != checked["digest"]


async def test_a_stronger_model_not_offered_is_never_read():
    run, _ = await _check(_draft(_writer()))
    run.shared_state["review_feedback"] = {"feedback": "Use a stronger model for Warning writer"}
    answer, _ = await _call("read_plan_answer", {}, run)
    assert answer["outcome"] == "revise"


# ── a model the provider refuses, and one the wallet can't pay ───────────────────────────────────────────────────

#: A model its provider refuses today (BED1, 10-27: Grok 4.7, its row enabled on dev, refused by AWS). The cloud hook
#: drops every refused `synapse.*` row from the menu (SAI2-P2), so here it is simply not on it.
REFUSED = "synapse.grok-4-7"
#: A model this workspace's wallet can't pay for now: on the menu, unusable, and the strongest within the ceiling, so
#: the most tempting pick and offer there is.
UNPAID = _entry("synapse.claude-sonnet-unpaid", reasoning=True, vision=True, out=14, pay=False)


@pytest.mark.parametrize("agent", [_writer(needs="reasoning"), _writer(stronger_helps=True),
                                   _writer(needs="vision", stronger_helps=True)])
async def test_a_refused_or_unpaid_model_is_never_picked_nor_offered(workspace, agent):
    workspace["now"] = [*MENU, UNPAID]
    _, checked = await _check(_draft(agent))
    pick = _pick(checked)
    for gone in (REFUSED, UNPAID.id):
        assert pick["model"] != gone and pick.get("stronger") != gone
        assert gone not in checked["review"] and not [o for o in checked["offers"] if gone in o]


async def test_a_wish_for_a_refused_model_is_asked_with_the_menus_names(workspace):
    workspace["now"] = [*MENU, UNPAID]
    _, checked = await _check(_draft(_writer(model_wish="Grok")))
    assert checked["outcome"] == "ask" and "Warning writer" in checked["question"]
    assert "No Grok model is available here" in checked["question"] and "synapse.claude-haiku" in checked["question"]
    assert REFUSED not in checked["question"]


async def test_a_wish_for_a_model_the_wallet_cant_pay_is_said_never_picked(workspace):
    workspace["now"] = [*MENU, UNPAID]
    _, checked = await _check(_draft(_writer(model_wish="claude sonnet unpaid")))
    assert not checked["plan"].get("models") and "build" not in checked["offers"]
    assert "Warning writer: synapse.claude-sonnet-unpaid can't be paid for now (the wallet)." in checked["issues"]


async def test_a_pick_gone_from_the_menu_since_the_yes_plans_again_then_stops_the_save(client, workspace):
    """F1375 B: the first refusal goes back to the planner in the same words (`refused`), the second stops."""
    run, _ = await _check(_draft(_writer(needs="reasoning")))
    run.shared_state["review_feedback"] = {"feedback": "Build it"}
    run.shared_state["plan_approval"], _ = await _call("approve_plan", {}, run)
    workspace["now"] = [e for e in MENU if e.id != "synapse.claude-sonnet"]  # refused since: off the menu
    first, _ = await _call("save_build", {}, run)
    assert first["outcome"] == "replan" and "synapse.claude-sonnet" in first["refused"], first
    saved, _ = await _call("save_build", {}, run)
    assert saved["outcome"] == "stopped" and "synapse.claude-sonnet" in saved["reason"], saved


# ── compiled, saved, priced ───────────────────────────────────────────────────────────────────────────────────────

async def test_compile_writes_each_agents_pick():
    from core.native_builder.compile import compile_plan

    _, checked = await _check(_draft(_writer(needs="reasoning")))
    compiled = compile_plan(O.Plan.model_validate(checked["plan"]))
    assert [a["model"] for a in compiled.agents] == ["synapse.claude-sonnet"]


async def test_ce1_prices_the_agent_step_on_its_pick():
    from core.native_builder.compile import compile_plan
    from core.orchestration.estimate import step_model

    _, checked = await _check(_draft(_writer(needs="reasoning")))
    compiled = compile_plan(O.Plan.model_validate(checked["plan"]))
    step = next(s for s in compiled.orchestration["steps"] if s["id"] == "tell")
    assert step_model(step, {a["id"]: a for a in compiled.agents}, "workspace-default") == "synapse.claude-sonnet"


async def test_the_save_makes_the_agent_on_its_pick(client):
    from core.routes.agents import load_user_agents

    run, _ = await _check(_draft(_writer(needs="reasoning")))
    run.shared_state["review_feedback"] = {"feedback": "Build it"}
    run.shared_state["plan_approval"], _ = await _call("approve_plan", {}, run)
    saved, _ = await _call("save_build", {}, run)
    assert saved["outcome"] == "saved", saved
    assert {a["name"]: a.get("model") for a in await load_user_agents()}["Warning writer"] == "synapse.claude-sonnet"


async def test_a_menu_that_moved_since_the_yes_plans_again_then_stops_the_save_and_says_so(client, workspace):
    """F1375 B: the first refusal names the agent, the pick it lost and the pick now, for the planner (`refused`);
    the second stops in the same words; neither saves the agent."""
    from core.routes.agents import load_user_agents

    run, _ = await _check(_draft(_writer(needs="reasoning")))
    run.shared_state["review_feedback"] = {"feedback": "Build it"}
    run.shared_state["plan_approval"], _ = await _call("approve_plan", {}, run)
    workspace["now"] = [e for e in MENU if e.id != "synapse.claude-sonnet"]
    for said, outcome in (("refused", "replan"), ("reason", "stopped")):
        saved, _ = await _call("save_build", {}, run)
        assert saved["outcome"] == outcome, saved
        assert "Warning writer" in saved[said] and "synapse.claude-sonnet" in saved[said]
        assert "synapse.claude-haiku" in saved[said]
        assert "Warning writer" not in {a["name"] for a in await load_user_agents()}


# ── the planner's pack ────────────────────────────────────────────────────────────────────────────────────────────

async def test_the_planners_pack_lists_the_menu_in_short_lines():
    run = OrchestrationRun(run_id="builder_models_pack", orchestration_id="orch_native_builder",
                           shared_state={builder_access.CALLER_KEY: {"user_id": "u1"}, "intent": "create"})
    await SharedState(run).checkpoint()
    pack, _ = await _call("read_plan_context", {}, run)
    lines = pack["models"]
    assert "synapse.claude-sonnet: calls tools, reasons, reads images, $15 per 1M output tokens" in lines
    assert "synapse.text-only: calls no tools, $0.1 per 1M output tokens" in lines


# ── the card ──────────────────────────────────────────────────────────────────────────────────────────────────────

def test_the_card_draws_each_model_offers_the_stronger_one_and_prices_the_picks():
    from pathlib import Path

    root = Path(__file__).resolve().parents[3] / "frontend/src/components/assistant"
    plan_ts, cards = (root / "plan.ts").read_text(), (root / "cards.tsx").read_text()
    assert "models: modelsOf(view)," in plan_ts and ">Models</span>" in cards
    # The press sends what `read_plan_answer` reads, and only for an offer the card made.
    assert "return `use a stronger model for ${agent}`;" in plan_ts
    assert build_plan._STRONGER.match("use a stronger model for Warning writer")
    assert "view.offers.filter((o) => o.startsWith('stronger:'))" in plan_ts
    # CE1's estimate prices an agent step on its pick, not the workspace default.
    assert "...(s.model ? { model: s.model } : {})," in plan_ts
