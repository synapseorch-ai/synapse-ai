"""Synapse AI's typed objects: one source for the backend, the panel and the tests (SAI2, D300; P1, F774).

A model writes language and judgement once, into one of these; code does everything that is a fact (DESIGN.md,
"Principle"). So each object has two halves, kept apart:

* **what a model writes**, validated here: `Request` (the quick model's classify), `PlanDraft` (the planner's
  `submit_plan`), `Change` (the change planner's `submit_change`);
* **what code fills in**, never accepted from a model: a `Plan`'s tool status, approvals, connections, what is
  missing and the plan room (`fill`), and a `Turn`, which code alone writes.

`PlanDraft.model_json_schema()` is the planner's tool schema; P0's probe measured eight models filling its first form
(`drafts/sai2/probe/plan_schema.py`; every model ≥ 14/15). The `workflow` step type is the engine's own now (E3).
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from core.models_orchestration import StepType
from core.native_builder import change_ops as _change_ops

#: The engine's step types, as a Plan names them.
STEP_TYPES = tuple(t.value for t in StepType)

_log = logging.getLogger(__name__)

_FIVE_FIELDS = re.compile(r"^\S+ \S+ \S+ \S+ \S+$")


# ── what a model writes: the Plan ─────────────────────────────────────────────────────────────────────────────────

class _Written(BaseModel):
    """A model's object: keys it adds that are not ours are dropped, never stored."""

    model_config = ConfigDict(extra="ignore")


class Step(_Written):
    id: str = Field(description="Short snake_case id, unique in the plan, e.g. draft_reply")
    type: Literal[STEP_TYPES] = Field(description="One of the engine's step types")  # type: ignore[valid-type]
    name: str = Field(description="A short name a person reads on the canvas")
    does: str = Field(description="One sentence: what this step does")
    agent: str | None = Field(None, description="agent/llm steps: a new agent's name from `agents`, or an existing "
                                                "agent's id")
    tool: str | None = Field(None, description="tool steps: the one tool it calls, by its exact name")
    prompt: str | None = Field(None, description="agent/llm steps: the instruction; human steps: the question to "
                                                 "the person; workflow steps: what the workflow is given")
    next: str | None = Field(None, description='The step id that follows, or "end"')
    branches: dict[str, str] | None = Field(None, description='if_else: {"true": id, "false": id}; '
                                                              "switch/evaluator: {label: id}")
    condition: str | None = Field(None, description="if_else: the condition in words or as an expression")
    loop_over: str | None = Field(None, description="loop steps: what list it goes through")
    body: list[str] | None = Field(None, description="loop: the step ids run for each item; parallel: the branch "
                                                     "step ids")
    workflow: str | None = Field(None, description="workflow steps: an inner workflow's title from "
                                                   "`inner_workflows`, or an existing workflow's id")
    config: dict[str, Any] | None = Field(None, description="Optional engine settings for this step, by their "
                                                            "names (e.g. human_fields, loop_max_iterations)")


class AgentBrowserPart(_Written):
    """A Browser agent's settings, as the agent editor's Browser section sets them (F1223)."""

    sites: list[str] = Field(default_factory=list, description="Hosts its pages may reach, e.g. portal.example.com "
                                                               "(empty: any public site)")
    sessions: list[str] = Field(default_factory=list, description="Sites it starts signed in on, with the person's "
                                                                  "kept session for each")
    mode: Literal["read_only", "co_drive"] = Field("read_only", description="read_only, or co_drive: it may type "
                                                                            "and send, with the person watching")
    #: The sites of `sessions` with no kept session here (code sets it, F1223): saved without, said on the card.
    sessions_missing: list[str] = Field(default_factory=list)
    recordings: list[str] = Field(default_factory=list, description="The workspace's recordings it may run, by name "
                                                                    "(F1259)")
    action_budget: int | None = Field(None, ge=1, le=200, description="The most browser actions it takes in one task "
                                                                      "(the editor's default: 40)")


class AgentTeamPart(_Written):
    """A team's way of working, as the agent editor's Team section sets it (F1259)."""

    mode: Literal["one_at_a_time", "team"] = Field("team", description="team: several members at once; "
                                                                      "one_at_a_time: members work in turn")
    at_once: int | None = Field(None, ge=1, le=50, description="team: how many members work at once (default 3)")
    task_budget: int | None = Field(None, ge=1, le=50, description="team: the most tasks it hands out (default 6)")
    spend_limit_usd: float | None = Field(None, gt=0, description="team: the most its members' work may cost, in "
                                                                  "dollars; leave it out for no limit")


class NewAgent(_Written):
    name: str
    type: Literal["agent", "team", "browser", "code", "orchestrator"] = Field(
        "agent", description="agent; team: a lead with members; browser: it works in websites in its own browser; "
                             "code: it works in the workspace's code repositories and databases; orchestrator: it "
                             "runs one workflow")
    purpose: str = Field(description="One or two sentences: what it is for")
    tools: list[str] = Field(default_factory=list, description="Exact tool names it needs")
    members: list[str] = Field(default_factory=list, description="team: the names or ids of its members")
    # What its model needs, never a model: code picks it from what this workspace can call (F966).
    needs: Literal["simple", "reasoning", "vision"] = Field(
        "simple", description="What its model needs: simple (most jobs), reasoning (careful judgement, tricky "
                              "writing), or vision (it reads images). Never a model's name: code picks the model")
    model_wish: str | None = Field(None, description="Only when the person named a model for this agent: their own "
                                                     "words, e.g. Claude, the cheapest, nova pro")
    long_inputs: bool = Field(False, description="True when it reads long inputs: whole documents, many pages or long "
                                                 "lists at once")
    stronger_helps: bool = Field(False, description="True when a stronger model would clearly do this job better: "
                                                    "the card offers one at its price, for the person to choose")
    why: str | None = Field(None, description="A few words on what the job needs from its model, shown beside the "
                                              "model, e.g. careful legal wording")
    # The agent editor's switches (F1223): what the editor sets, the plan sets, and every save door checks.
    memory: bool = Field(False, description="True when it must remember people, preferences or past results across "
                                            "runs and conversations")
    knowledge: list[str] = Field(default_factory=list, description="The knowledge bases it answers from, by their "
                                                                   "names in `knowledge_bases`")
    browser: AgentBrowserPart | None = Field(None, description="browser agents only: where it may go and the sites "
                                                               "it starts signed in on")
    servers: list[str] = Field(default_factory=list, description="Connected MCP servers, by name: it is given every "
                                                                 "tool each has now")
    # Every other setting the agent editor has (F1259; `agent_census`): each checked as the editor's save checks it.
    skills: list[str] = Field(default_factory=list, description="The workspace's skills it loads, by name")
    planning: bool = Field(True, description="False for an agent that answers straight away, without planning first")
    skip_default_tools: bool = Field(False, description="True when it gets only the tools named here, none of the "
                                                        "product's own")
    helpers: bool = Field(False, description="True when it may hand parts of a job to helpers that run beside it; "
                                             "code picks their model")
    max_turns: int | None = Field(None, ge=1, le=200, description="The most turns one task may take; leave it out "
                                                                  "for the workspace's")
    turn_limit_mode: Literal["ask", "continue", "stop"] | None = Field(
        None, description="At its turn limit: ask the person, continue, or stop; leave it out for the workspace's")
    reasoning_effort: Literal["off", "low", "medium", "high"] | None = Field(
        None, description="How hard its model thinks, where the model can; leave it out for the workspace's")
    max_output_tokens: int | None = Field(None, description="The most its model writes in one answer; leave it out "
                                                            "for the workspace's")
    team: AgentTeamPart | None = Field(None, description="team agents only: how its members work")
    boards_own: bool | None = Field(None, description="False when it shares a board instead of having its own")
    repos: list[str] = Field(default_factory=list, description="code agents: the workspace's repositories, by name")
    db_configs: list[str] = Field(default_factory=list, description="code agents: the workspace's databases, by name")
    workflow: str | None = Field(None, description="orchestrator agents: the workflow it runs, by its name or id")


class ModelPick(BaseModel):
    """The model code picked for a new agent (F966), and why; a stronger one it offers, at its price."""

    model: str
    name: str
    reason: str
    stronger: str | None = None
    stronger_price: str | None = None


class Schedule(_Written):
    #: How it is timed, by the same names and values as the Schedules screen and the change ops (F1267; D340 F).
    schedule_type: Literal["cron", "interval"] = Field("cron", description="cron: at set times; interval: every so "
                                                                           "often, counted from when it was made")
    cron: str = Field("", description="cron: five-field cron on the schedule's own clock, the time as they said "
                                      "it (7 in the morning is 0 7 * * *), never converted to UTC")
    interval_value: int | None = Field(None, description="interval: how many units between runs, e.g. 15")
    interval_unit: Literal["minutes", "hours", "days"] | None = Field(None, description="interval: minutes, hours or "
                                                                                         "days")
    missed_run_policy: str = Field("skip", description="A time it missed (the machine was off): skip, or "
                                                       "run_immediately to run it once as soon as it can")
    timezone: str | None = Field(None, description="Only a timezone the person named, as a name like "
                                                   "America/New_York: the cron is read on it. Left out, the schedule "
                                                   "runs on the person's own clock")
    says: str = Field(description="The schedule in words, e.g. every day at 7:00")
    input: str | None = Field(None, description="What each scheduled run starts with: the task, with what it is "
                                                "about (F155)")


class InnerWorkflow(_Written):
    title: str
    does: str
    steps: list[Step]


class Caps(_Written):
    credits: float | None = Field(None, description="The most credits one run may use")
    dollars: float | None = Field(None, description="The most model dollars one run may spend")


class BoardPart(_Written):
    name: str
    purpose: str = ""
    #: A shared board already here (code sets it, F1214): given to the workflow, never made again.
    existing: bool = False


class KnowledgePart(_Written):
    """A knowledge base the build makes, or one here it uses, as F1214's board is (F1224): only while Knowledge is
    on; its files are ones the person already has in the Vault."""

    name: str
    purpose: str = ""
    files: list[str] = Field(default_factory=list, description="Vault paths of files the person already has, "
                                                               "e.g. help/returns.md")
    #: A base already here of that name (code sets it): used, never made again.
    existing: bool = False
    #: Paths of `files` not in the Vault (code sets it): said on the card, never added.
    files_missing: list[str] = Field(default_factory=list)
    urls: list[str] = Field(default_factory=list, description="Public web pages it is filled from (D340 F): pages, or "
                                                              "one sitemap's URL, or one site's start URL")
    kind: Literal["page", "sitemap", "site"] = Field("page", description="page: each URL is one page; sitemap: the "
                                                                         "pages a sitemap lists; site: a crawl from "
                                                                         "the URL")
    #: The most pages a sitemap or a site brings (code sets it): Knowledge's own crawl limit.
    pages_cap: int = 0


class ApiPart(_Written):
    """The person's own API, from the OpenAPI spec they gave (F1225): the build makes a tool for each operation its
    steps and agents use, as it makes a read API's (F944), and asks for its key on a card after the yes."""

    name: str = Field(description="A short name for the API, e.g. Orders API")
    spec: str = Field(description="The spec as the person gave it: its https URL, or its Vault path")
    #: The tools the plan uses from it, by the names `read_api_spec` gave (code sets it).
    tools: list[str] = Field(default_factory=list)
    #: Why its spec cannot be used here (code sets it): said on the card.
    problem: str = ""


class McpServerPart(_Written):
    """A remote MCP server the person gave by its URL (F1225). Its tools are known only once it is connected, so its
    Connect card comes before the plan, and the plan is drawn again on its real tools (Naveen, 10-29)."""

    name: str = Field(description="A short name for the server, e.g. Tracker")
    url: str = Field(description="Its http or https URL, as the person gave it")
    #: Connected here, by that URL (code sets it): its tools are in the workspace's `servers`.
    connected: bool = False


class EvalCriterion(_Written):
    name: str
    weight: float = 1.0
    guidance: str = Field("", description="What a good result does, for the judge")


class EvalCase(_Written):
    name: str = ""
    input: str = Field(description="What a run starts with, as a person would give it")
    notes: str = Field("", description="What a good result does with it, for the person reading the scores")


class EvalPart(_Written):
    """The workflow's eval, made with it (F1255, D340 F): a rubric a judge scores each finished run by, and a golden
    set of test inputs a person runs to see whether a later change made it worse. Its cases are saved only as the
    card shows them, on the yes (Naveen, 10-30)."""

    name: str = Field(description="The rubric's name, e.g. Post quality")
    criteria: list[EvalCriterion] = Field(description="What a good result is, each with its weight, from what the "
                                                      "workflow is for")
    scale_max: float = Field(10.0, description="The top of the score scale (0 to it)")
    floor: float | None = Field(None, description="A score below it is a regression")
    cases: list[EvalCase] = Field(default_factory=list, description="When the workflow takes an input: 3 to 10 test "
                                                                     "inputs (a typical one, an edge case, a hard one); "
                                                                     "none when it takes no input")
    replace_rubric: bool = Field(False, description="An update of a workflow scored by a rubric already: true only "
                                                    "when the ask is about how it is judged; else its rubric is kept")
    judge_every: int = Field(1, description="Judge its first finished run, then 1 run in this many: 1 for a workflow "
                                            "run now and then; more for one a schedule runs often, as each judged "
                                            "run is one more model call (up to 100)")
    #: The rubric an updated workflow is scored by now, by name (code sets these): kept, or replaced by this one.
    keeps: str = ""
    replaces: str = ""
    #: The model that judges each finished run (code sets it): the rubric's, else the workspace's judge or default.
    judge: str = ""
    #: What one judged run costs, in the card's words ("about $0.002"), from the judge's rate card (code sets it).
    judge_cost: str = ""


class Edit(_Written):
    """An update Plan's change to an existing workflow, by step id."""

    op: Literal["add_step", "update_step", "remove_step", "set_meta"]
    step_id: str | None = Field(None, description="update_step / remove_step: the step's id")
    after: str | None = Field(None, description="add_step: the step id it goes after")
    step: Step | None = Field(None, description="add_step: the new step")
    fields: dict[str, Any] = Field(default_factory=dict, description="update_step / set_meta: what changes")


class PlanDraft(_Written):
    """What the planner writes (`submit_plan`)."""

    kind: Literal["create", "update"]
    title: str
    summary: str = Field(description="One or two sentences in the person's words")
    steps: list[Step] = Field(default_factory=list)
    edits: list[Edit] = Field(default_factory=list, description="update: the changes, by step id")
    agents: list[NewAgent] = Field(default_factory=list, description="New agents only")
    reuse_agents: list[str] = Field(default_factory=list, description="Ids of existing agents the steps use")
    schedule: Schedule | None = None
    inner_workflows: list[InnerWorkflow] = Field(default_factory=list, description="Workflows this one runs, "
                                                                                    "saved first")
    caps: Caps | None = None
    boards: list[BoardPart] = Field(default_factory=list)
    knowledge: list[KnowledgePart] = Field(default_factory=list, description="Knowledge bases this build makes or "
                                                                             "uses, by name: what its agents answer "
                                                                             "from and its retrieve/ingest steps name")
    apis: list[ApiPart] = Field(default_factory=list, description="The person's own APIs, from the OpenAPI spec "
                                                                  "they gave: read it with read_api_spec first, and "
                                                                  "use its tools by the names that gives")
    mcp_servers: list[McpServerPart] = Field(default_factory=list, description=(
        "A remote MCP server the person gave by URL that is not in `servers` yet: plan no step on its tools; it is "
        "connected first, and you plan again on its tools"))
    eval: EvalPart | None = Field(None, description="Its eval: a rubric its runs are scored by and golden test "
                                                    "cases, made with it")
    not_carried_over: list[str] = Field(default_factory=list, description="A pasted export: what it had that the "
                                                                          "plan does not carry over")
    settings: dict[str, Any] | None = Field(None, description="Optional workflow settings by their engine names: "
                                                              "state_schema, result_key")


# ── what code fills in ─────────────────────────────────────────────────────────────────────────────────────────────

class ToolStatus(BaseModel):
    name: str
    #: present: a run could call it now; found: research found who provides it (connect first); missing: neither.
    status: Literal["present", "found", "missing"]
    provider: str | None = None
    via: Literal["catalog_app", "mcp_server", "http_tool", "none"] = "none"
    #: What the research said of it: why a provider has no way in, for the card (the hand-off's A6).
    detail: str | None = None
    #: `http_tool`: the API spec the research read, to make the tool from (F943, F944).
    api: dict | None = None
    #: Its API takes only the person's own sign-in (F1121's OAuth), so no tool is made: the card says why (F1427).
    signs_in: bool = False


def only_reads(status: ToolStatus) -> bool:
    """A tool this build makes that only reads, which runs without asking first (F1424, Naveen): an `http_tool` it
    makes after the yes (the research's spec or the person's own), a GET (no body), taking no key (no header), with no
    host part a card fills; `build_save._tool_call` locks its base. Never a tool already here: its ask is its own."""
    api = status.api or {}
    return (status.status == "found" and status.via == "http_tool" and api.get("keyless") is True
            and not api.get("host_params") and str((api.get("endpoint") or {}).get("method") or "").upper() == "GET")


class Approvals(BaseModel):
    """Every tool the plan uses that changes something outside: each asks first unless the person moved it."""

    asks_first: list[str] = Field(default_factory=list)
    acts_without_asking: list[str] = Field(default_factory=list)


class Gate(BaseModel):
    """One approval the run asks for, or one block: who (an agent's name, or a Tool step's), before which tool, and
    what set it — this build (a new agent's confirm), the agent's own tool policy, or the workspace's (F964)."""

    who: str
    tool: str
    decision: Literal["confirm", "deny"]
    set_by: Literal["build", "agent", "workspace"]
    #: `step`: a Tool step's own call, decided by the workspace's policy (and its agent's, if it names one).
    kind: Literal["agent", "step"] = "agent"
    #: The Tool step's id, for a `step` gate: on an update, only a step it writes can block it (SAI2's review, S2).
    step: str | None = None


class Plan(PlanDraft):
    """A PlanDraft with what code knows about it. Only `from_draft` and `fill` make one from a model's output."""

    model_config = ConfigDict(extra="ignore")

    tools: list[ToolStatus] = Field(default_factory=list)
    approvals: Approvals = Field(default_factory=Approvals)
    connections_needed: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    #: Why the plan does not fit the workspace's plan (cloud's F154), or None.
    room: str | None = None
    #: Every approval the run will ask for, and every block, as the engine's policy decides them (F964).
    gates: list[Gate] = Field(default_factory=list)
    #: Each new agent's model, by its name, picked in code (F966): never the planner's.
    models: dict[str, ModelPick] = Field(default_factory=dict)
    #: No step of a run calls a model (F967): the card says it runs on tools only. Set by `fill`.
    calls_no_model: bool = False
    #: The tool steps whose inputs a model fills in, by id: none set in `config.tool_arguments` (F967). Set by `fill`.
    model_fills: list[str] = Field(default_factory=list)
    #: What one run would cost in model calls, in the card's words (D340 D3, `estimate.of`): a note, never a block.
    run_cost: str = ""
    #: Each knowledge base its new agents answer from, by name → id (F1223). Set by `examine`.
    knowledge_ids: dict[str, str] = Field(default_factory=dict)
    #: Each repository, database, workflow and recording its new agents name, as `kind:name` → id (F1259). Set by
    #: `examine`.
    refs: dict[str, str] = Field(default_factory=dict)
    #: What each step does, by its id, in code's words from its parts (F1228, `parts_words`). Set by `fill`.
    step_words: dict[str, str] = Field(default_factory=dict)
    #: The plan's own parts in those words: its boards, knowledge bases, APIs, apps and schedule. Set by `fill`.
    part_words: list[str] = Field(default_factory=list)
    #: What is missing or off, besides a missing tool or a connection to make, each with where it is put right.
    off: list[str] = Field(default_factory=list)
    #: What this plan changed from the one the person last saw: a step added or removed, its tool, a shown input
    #: (F1245, `build_plan.since_last`). Set by `check_build`.
    since_last: list[str] = Field(default_factory=list)
    #: An older plan's open questions, as its stored card shows them. A plan carries none since F1253: what only the
    #: person can say is asked before any plan (`ask_person`'s form; `build_plan._asked_first`).
    questions: list[str] = Field(default_factory=list)

    @classmethod
    def from_draft(cls, raw: dict[str, Any] | PlanDraft) -> "Plan":
        """A model's object as a Plan: read as a PlanDraft first, so a code field it wrote is dropped."""
        draft = raw if isinstance(raw, PlanDraft) and not isinstance(raw, Plan) else PlanDraft.model_validate(
            raw if isinstance(raw, dict) else raw.model_dump())
        return cls.model_validate(draft.model_dump())


def plan_schema() -> dict[str, Any]:
    """The planner's tool schema: what a model writes, and nothing code fills in."""
    return PlanDraft.model_json_schema()


# ── a request, a change, a turn ───────────────────────────────────────────────────────────────────────────────────

#: What an answer may read (F976): nothing, this workspace, or the product's help.
NEEDS = ("none", "workspace", "help")


class Request(_Written):
    """The quick model's classify (`classify_request`): four labels, and what code needs to override one."""

    kind: Literal["answer", "change", "build", "research"]
    #: The product feature the ask is about, typed (D340: one field for every feature, a census row's id), never read
    #: from words: `evals` goes to Evals' own ops, never a build (F1255). D0 maps it onto its act route.
    feature: Literal["evals"] | None = Field(
        None, description="evals when the ask is to measure or read how well a workflow does: an eval, a rubric "
                          "scoring its runs, golden test cases, running them, or its scores. Else leave out")
    providers: list[str] = Field(default_factory=list, description="Outside services the message names")
    also: str | None = Field(None, description="A second request in the same message, in its words, if any")
    language: str | None = Field(None, description="The language the person wrote in, e.g. en, hi")
    #: A fact from outside that changes over time, asked for now (F1382): code sends it to the talk step, which offers
    #: to set up the workflow that gets it. Code checks it against what the message names (`classify.override`).
    live_fact: str | None = Field(
        None, description="A fact from outside this workspace that changes over time, which the message asks to be "
                          "told now (an exchange rate, a flight's status, a score), in its words. Empty when it asks about this "
                          "workspace or the product, or for something to be built")
    #: Whether this message goes on with the previous one's request (F1226): the questions it asked carry only when
    #: true. Left out or unsure starts afresh.
    continues: bool | None = Field(
        None, description="True only when this message goes on with the request the person's previous message made "
                          "(answers it, says it again in other words, or adds to it); false when it asks for "
                          "something else; leave out when unsure")
    #: What the answer reads (F976). Left out, code decides alone: a model that says nothing has not said "none".
    needs: Literal[NEEDS] | None = Field(  # type: ignore[valid-type]
        None, description="What an answer needs: none (talk, ideas, thanks), workspace (what exists or happened "
                          "here), help (how the product or its API works)")
    #: How much a build asks of a model (F1153): a label only; code decides whether to offer a stronger one.
    effort: Literal["light", "heavy"] | None = Field(
        None, description="For a build only: heavy when it asks for many distinct steps, agents working together, "
                          "or a workflow inside another; else light")
    why: list[Literal["many_steps", "team", "nested"]] = Field(
        default_factory=list, description="For a heavy build, which: many_steps (more than about 6 distinct actions, "
                                          "or branches and loops together), team (agents working together, a team "
                                          "or a board), nested (a workflow inside a workflow)")


class RequestAtPlan(Request):
    """The classify while a plan waits at its review (F1241's follow-up, 8a 10-30): `continues` is asked plainly and is
    required, true or false. Probe E found prod's quick model leaves it out every time when it may (0/3 on
    nova-pro), so F1241's carry never fired; asked as a required field it has to say."""

    continues: bool = Field(  # type: ignore[assignment]
        description="Does this message change or add to the waiting plan (true), or ask for something else (false)?")


#: The route entries code hands a request to: a contract with the routes' owners (EDIT-LEDGER.md).
ENTRIES = ("step_answer", "step_change_context", "step_plan_room", "step_fetch_existing", "step_research",
           "step_not_yours")


class Routed(Request):
    """A `Request` after code's overrides (`classify_request`): code alone writes this, never a model.

    `kind` and `needs` are code's final reading; `model_kind` / `model_needs` keep what the model said, and
    `overridden_by` names the rule that moved it, so the router measure can tell the two apart.
    """

    model_config = ConfigDict(extra="forbid")

    needs: Literal[NEEDS]  # type: ignore[valid-type]
    route: Literal[ENTRIES]  # type: ignore[valid-type]
    overridden_by: str | None = None
    model_kind: str | None = None
    model_needs: str | None = None


#: The ops a change may hold: the button acts (`ACTIONS`), the writes, and two hand-overs to another route
#: (`edit_steps`, `create`). Closed, and one table's (`change_ops.OPS`, F792): a change with no op that fits is
#: answered "can't" with the nearest thing that works (P0's finding 1).
CHANGE_OPS = _change_ops.PLANNER_OPS
WRITE_OPS = tuple(n for n in CHANGE_OPS if not _change_ops.OPS[n].is_act)


class Target(_Written):
    kind: Literal[_change_ops.TARGET_KINDS]  # type: ignore[valid-type]
    id: str | None = Field(None, description="Its id when known; else its name")
    name: str | None = None


class ChangeOp(_Written):
    op: Literal[CHANGE_OPS]  # type: ignore[valid-type]
    target: Target
    fields: dict[str, Any] = Field(default_factory=dict)
    says: str = Field("", description="The change in the person's words")

    @property
    def is_act(self) -> bool:
        """A button act: it goes on a card the person presses, never through the plan review (F755)."""
        return _change_ops.OPS[self.op].is_act


class AskedPart(_Written):
    """One part of what the person asked, and what the change does about it (F1364): its ops, or why not here."""

    part: str = Field(description="The part, in the person's words")
    ops: list[int] = Field(default_factory=list, description="The numbers of the ops that do it (1 is the first)")
    why_not: str | None = Field(None, description="Why it is not done by this change, when no op does it")


class Change(_Written):
    """What the change planner writes (`submit_change`)."""

    ops: list[ChangeOp] = Field(default_factory=list)
    #: Every part of the ask, each with the ops that do it or why not (F1364: cap-02's two parts dropped unsaid).
    asked: list[AskedPart] | None = None
    questions: list[str] = Field(default_factory=list)
    #: "Undo that": the board card of the change to undo. Code builds the inverse ops from its record (F797).
    undo_of: str | None = None

    @property
    def has_writes(self) -> bool:
        return any(not op.is_act for op in self.ops)


class Turn(BaseModel):
    """Code's reading of a message before any model (`read_turn`): which route, and what it resolved.

    `pressed` and `yes_without_review` (F976) are answered by code alone, in `say`: a press the last turn's card
    recorded, and a yes with nothing waiting for one. `plan_answer` stays the panel's (F759): a message typed while a
    review waits sets the run aside there, so a new turn never finds one waiting.
    """

    route: Literal["pick", "card_label", "plan_answer", "pressed", "yes_without_review", "next_request", "classify",
                   "effort_continue", "effort_build", "offer_pick", "offer_taken", "tool_test"]
    #: What a pick chose: an act's call (code puts its card up) or a change's chosen id (the change route takes it).
    picked: str | dict[str, Any] | None = None
    card: str | None = None
    target: dict[str, Any] | None = None
    say: str | None = None
    #: Where the turn goes next (F978): code said it all, back to the change route with a pick, or on to classify.
    then: Literal["said", "change", "classify", "build"] = "classify"
    #: The message classify reads: the person's, the request a yes took from the queue (`next_request`), or the ask a
    #: pick from the last reply's options goes back on, with the choice (`offer_pick`, F1347), or the build a yes to the
    #: offer for a live fact asks for (`offer_taken`, F1382).
    message: str | None = None


# ── the checks ────────────────────────────────────────────────────────────────────────────────────────────────────

@dataclass
class Inventory:
    """The workspace as a check reads it: agents and workflows by id → name, and its tool names."""

    agents: dict[str, str] = field(default_factory=dict)
    workflows: dict[str, str] = field(default_factory=dict)
    tools: set[str] = field(default_factory=set)


def read(raw: dict[str, Any]) -> tuple[Plan | None, list[str]]:
    """A model's `submit_plan` arguments as a Plan, or the errors to send back for a retry (one line each)."""
    try:
        return Plan.from_draft(raw), []
    except ValidationError as e:
        return None, [f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors()[:8]]


def _as_object(raw: Any) -> Any:
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except ValueError:
            return None
    return raw


def wrapped_in(raw: Any) -> str | None:
    """The one key of `raw` (a planner's arguments), not a plan field itself, whose object holds plan fields: the
    shape `check_draft` takes, sent to `submit_plan` (F1441). None when there is no one such key."""
    raw = _as_object(raw)
    if not isinstance(raw, dict):
        return None
    keys = [k for k, v in raw.items() if k not in PlanDraft.model_fields
            and isinstance(o := _as_object(v), dict) and set(o) & set(PlanDraft.model_fields)]
    return keys[0] if len(keys) == 1 else None


def plan_args(raw: Any) -> Any:
    """A planner's plan arguments with a plan sent inside one wrapper key read as the plan (F1441: Haiku sent
    `{"draft": {...}}` to `submit_plan`, the shape `check_draft` takes): only when the top level holds no plan field
    and that one object has a kind and a title, steps or edits. The keys beside it (its questions) are kept, under the
    plan's own. Anything else is `raw` as it came (a JSON string read), for the check to say what it lacks."""
    raw = _as_object(raw)
    key = wrapped_in(raw)
    if key is None or set(raw) & set(PlanDraft.model_fields):
        return raw
    inner = _as_object(raw[key])
    if "kind" not in inner or not {"title", "steps", "edits"} & set(inner):
        return raw
    return {**{k: v for k, v in raw.items() if k != key}, **inner}


def _targets(step: Step) -> list[str]:
    return ([step.next] if step.next else []) + list((step.branches or {}).values()) + list(step.body or [])


def tool_names(plan: PlanDraft) -> list[str]:
    """Every tool the plan names: its steps', its inner workflows' and its new agents', in order, once each."""
    names: list[str] = []
    for s in [*plan.steps, *(x for w in plan.inner_workflows for x in w.steps)]:
        if s.tool:
            names.append(s.tool)
    for a in plan.agents:
        names += a.tools
    return list(dict.fromkeys(n for n in names if n))


def settled(steps: list[Step]) -> list[Step]:
    """A Plan's steps with what it leaves unsaid decided, as code can: models list steps in order and leave `next`
    out (P0's plans, often), and a list is an order.

    * A step with no `next` that does not branch and is not inside a Loop or a Parallel goes to the next step in
      the list that is not inside one either — or ends the run when it is the last (`"end"`), or when that step is
      an End another arm names (`arm_ends`): the approved arm never ends on "End — Not Approved" (F1429).
    * A Parallel with no join joins at the merge all its branches lead to, or else as any step would.
    * A container's body steps go nowhere of their own (F124), and an End step goes nowhere at all.
    """
    by_id = {s.id: s for s in steps}
    bodies = {b for s in steps if s.type in ("loop", "parallel") for b in s.body or []}
    owned = arm_ends(steps)
    out: list[Step] = []
    for i, s in enumerate(steps):
        if s.next is None and s.type != "end" and s.id not in bodies and not (
                s.branches and s.type in ("if_else", "switch", "evaluator")):
            join = None
            if s.type == "parallel":
                joins = {by_id[b].next for b in s.body or [] if b in by_id}
                if len(joins) == 1 and (only := next(iter(joins))) in by_id and by_id[only].type == "merge":
                    join = only
            if join is None:
                join = next((x.id for x in steps[i + 1:] if x.id not in bodies), "end")
                join = "end" if join in owned else join
            s = s.model_copy(update={"next": join})
        out.append(s)
    return out


def arm_ends(steps: list[Step]) -> set[str]:
    """The End steps an arm of a branching step names: that arm's own, which no other step reaches by list order and
    no plain "end" compiles to (F1429). An arm's `"end"` is the plain end, even where an End step's id is `end`
    (a compiled plan's own, read back: bld-07)."""
    ends = {s.id for s in steps if s.type == "end"}
    return {t for s in steps if s.type in ("if_else", "switch", "evaluator") for t in (s.branches or {}).values()
            if t in ends and t != "end"}


def _unreached(steps: list[Step], where: str) -> list[str]:
    """Steps no path from the first one reaches: a plan whose parts are not joined up runs only its first part."""
    steps = settled(steps)
    by_id = {s.id: s for s in steps}
    seen: set[str] = set()
    todo = [steps[0].id] if steps else []
    while todo:
        sid = todo.pop()
        if sid in seen or sid not in by_id:
            continue
        seen.add(sid)
        todo += _targets(by_id[sid])
    return [f"{where}step {s.id}: no step leads to it, so it never runs: lead to it from another step, or drop it"
            for s in steps if s.id not in seen and s.type != "end"]


def check_structure(plan: PlanDraft, inventory: Inventory) -> tuple[list[str], list[str]]:
    """(errors, notes). Errors go back to the model for a retry; notes are facts code fills in (a tool nothing
    provides is a note, not an error: finding who does is the lookup's job, not the model's)."""
    errors: list[str] = []
    notes: list[str] = []
    ids = [s.id for s in plan.steps]
    if plan.kind == "create" and not plan.steps:
        errors.append("steps: a new workflow needs at least one step")
    if plan.kind == "update" and not (plan.edits or plan.steps):
        errors.append("edits: an update names at least one change")
    if len(set(ids)) != len(ids):
        errors.append("steps: two steps share an id")
    known = set(ids) | {"end"}
    agents = {a.name for a in plan.agents} | set(inventory.agents) | set(plan.reuse_agents)
    for s in plan.steps:
        for t in _targets(s):
            if t not in known:
                errors.append(f"step {s.id}: goes to '{t}', which is not a step id")
        if s.type == "agent" and not s.agent:
            errors.append(f"step {s.id}: an agent step names its agent")
        if s.agent and s.agent not in agents:
            errors.append(f"step {s.id}: agent '{s.agent}' is neither new in `agents` nor an existing id")
        if s.type == "workflow" and not s.workflow:
            errors.append(f"step {s.id}: a workflow step names the workflow it runs")
        if s.type == "tool" and not s.tool:
            errors.append(f"step {s.id}: a tool step names its tool")
        if s.type == "if_else" and not (s.branches and {"true", "false"} <= set(s.branches)):
            errors.append(f"step {s.id}: if_else needs branches true and false")
        if s.type in ("switch", "evaluator") and not s.branches:
            errors.append(f"step {s.id}: {s.type} needs branches")
        if s.type == "loop" and not s.body:
            errors.append(f"step {s.id}: a loop names its body steps")
        if s.type == "human" and not s.prompt:
            errors.append(f"step {s.id}: a human step says what it asks")
        if s.type == "llm" and not (s.prompt or "").strip():
            # An llm step has no agent's instructions: with no prompt its model has only the step's name (HOT2's read
            # of the Weekly Twitter Post plan). An agent step runs on its agent's instructions, an evaluator on its
            # routes, so either may leave its prompt out.
            errors.append(f"step {s.id}: an llm step says what it asks its model: write its prompt")
        if s.branches and s.type not in ("if_else", "switch", "evaluator"):
            errors.append(f"step {s.id}: a {s.type} step cannot branch; branch in an if_else, a switch or an "
                          "evaluator step after it")
    errors += _unreached(plan.steps, "")
    flows = {w.title for w in plan.inner_workflows} | set(inventory.workflows)
    for s in plan.steps:
        if s.workflow and s.workflow not in flows:
            errors.append(f"step {s.id}: workflow '{s.workflow}' is neither in `inner_workflows` nor an existing id")
    # Run by the workflow or by another of its inner workflows (a nested build's own chain).
    used = {s.workflow for s in [*plan.steps, *(x for w in plan.inner_workflows for x in w.steps)] if s.workflow}
    for w in plan.inner_workflows:
        if w.title not in used:
            errors.append(f"inner workflow '{w.title}' is never run: add a workflow step that runs it, or drop it")
        inner = {x.id for x in w.steps} | {"end"}
        errors += _unreached(w.steps, f"inner workflow '{w.title}' ")
        for x in w.steps:
            for t in _targets(x):
                if t not in inner:
                    errors.append(f"inner workflow '{w.title}' step {x.id}: goes to '{t}', which is not one of its "
                                  "step ids")
    for name in tool_names(plan):
        if name not in inventory.tools:
            notes.append(f"missing tool: {name}")
    if plan.schedule:
        errors += schedule_issues(plan.schedule)
    return errors, sorted(set(notes))


def schedule_issues(schedule: Schedule) -> list[str]:
    """A plan's schedule held to the Schedules screen's own fields, through the change route's one check of them
    (`change_ops._field_issue` on create_schedule, F1267): its type, its cron or interval, what it does with a missed
    time."""
    from core.native_builder import change_ops as C

    op, errors = C.OPS["create_schedule"], []
    if schedule.schedule_type == "cron":
        if not _FIVE_FIELDS.match(schedule.cron.strip()):
            errors.append(f"schedule.cron: '{schedule.cron}' is not five fields")
    else:
        for key in ("interval_value", "interval_unit"):
            if (said := C._field_issue(op, key, getattr(schedule, key))) is not None:
                errors.append(f"schedule.{key}: {said}")
    if (said := C._field_issue(op, "missed_run_policy", schedule.missed_run_policy)) is not None:
        errors.append(f"schedule.missed_run_policy: {said}")
    return errors


async def _gates(plan: Plan, server_module: Any, custom: list, agents: dict[str, dict] | None,
                 asks: set[str]) -> list[Gate]:
    """What the run will ask for and what it blocks, decided as the engine decides each call
    (`tool_policy.decide(tool, resolve_policy(agent, settings))`), for (F964; REL2's demo):

    * the plan's new agents, with the confirm the compile gives each of their tools in `asks`;
    * the agents it reuses, and the members of a team it reuses: every tool the run would give them (`all`, groups
      and servers included, as the engine aggregates them), under their own policy;
    * its Tool steps, under their agent's policy (a new agent's as compiled) and the workspace's.
    """
    from core.config import load_settings
    from core.tool_policy import TOOL_KEY, decide, resolve_policy
    from core.tools import ALL_TOOLS_TOKEN_BUDGET, aggregate_all_tools, resolve_one_tool

    if agents is None:
        from core.routes.agents import load_user_agents

        agents = {a["id"]: a for a in await load_user_agents() if a.get("id")}
    settings = load_settings()
    resolved: dict[str, Any] = {}

    async def tool(name: str):
        if name not in resolved:
            try:
                resolved[name] = await resolve_one_tool(server_module, name, {"tools": [name]}, custom)
            except Exception:  # noqa: BLE001 — a lookup that cannot run decides nothing
                resolved[name] = None
        return resolved[name]

    async def held_by(agent: dict) -> list[Any]:
        """The tools a run gives a reused agent, as the engine aggregates them (SAI2's review, S4)."""
        try:
            return (await aggregate_all_tools(server_module, agent, custom, budget=ALL_TOOLS_TOKEN_BUDGET))[0]
        except Exception:  # noqa: BLE001 — a server that cannot list is not a gate
            return [t for n in agent.get("tools") or [] if isinstance(n, str) and (t := await tool(n)) is not None]

    def set_by(agent: dict | None, category: str, name: str) -> str:
        """The agent's own policy when it holds the key that decided, else the workspace's (SAI2's review, S3)."""
        own = (agent or {}).get("tool_policy") or {}
        key = f"{TOOL_KEY}{name}" if category == "tool" else "undeclared" if category == "changes" else category
        return "agent" if own.get(key) in ("allow", "confirm", "deny") else "workspace"

    def compiled(agent_name: str) -> dict:
        """A new agent's policy as the compile writes it: confirm for each of its tools in `asks`."""
        return {"tool_policy": {f"{TOOL_KEY}{t}": "confirm" for t in new[agent_name].tools if t in asks}}

    gates: list[Gate] = []
    seen: set[tuple[str, str, str]] = set()

    def add(who: str, name: str, decision: str, by: str, *, kind: str = "agent", key: str = "") -> None:
        # Keyed by the agent's id or the step's, not its name: two agents of one name are two (SAI2's nit).
        if decision in ("confirm", "deny") and (kind, key or who, name) not in seen:
            seen.add((kind, key or who, name))
            gates.append(Gate(who=who, tool=name, decision=decision, set_by=by, kind=kind,
                              step=key if kind == "step" else None))

    new = {a.name: a for a in plan.agents}
    steps = [*plan.steps, *(x for w in plan.inner_workflows for x in w.steps)]
    refs = [s.agent for s in steps if s.type == "agent" and s.agent]
    for ref in refs:  # a team's members run too, each under its own policy: a new team's and a reused one's
        members = new[ref].members if ref in new and new[ref].type == "team" else \
            (agents.get(ref) or {}).get("delegate_agent_ids") or []
        refs += [m for m in members if m not in refs]
    for ref in dict.fromkeys(refs):
        if ref in new:
            for name in new[ref].tools:
                if (t := await tool(name)) is None:
                    continue
                inherited, category = decide(t, resolve_policy({}, settings))
                if inherited == "deny":
                    add(ref, name, "deny", "workspace", key=ref)
                elif name in asks:
                    add(ref, name, "confirm", "build" if inherited == "allow" else "workspace", key=ref)
                else:
                    add(ref, name, inherited, "workspace", key=ref)
        elif (agent := agents.get(ref)) is not None:
            policy = resolve_policy(agent, settings)
            for t in await held_by(agent):
                name = getattr(t, "name", None) or (t.get("name") if isinstance(t, dict) else None)
                if name:
                    decision, category = decide(t, policy)
                    add(agent.get("name") or ref, name, decision, set_by(agent, category, name), key=ref)
    for s in steps:
        if s.type == "tool" and s.tool and (t := await tool(s.tool)) is not None:
            if s.agent in new:
                # Decided as the new agent's own tools are: the workspace's first, then the compile's own confirm,
                # which is "this build" only where the workspace would let it act (SAI2's re-check of S3).
                inherited, _ = decide(t, resolve_policy({}, settings))
                if inherited == "deny":
                    add(s.name, s.tool, "deny", "workspace", kind="step", key=s.id)
                elif s.tool in asks and s.tool in new[s.agent].tools:
                    add(s.name, s.tool, "confirm", "build" if inherited == "allow" else "workspace", kind="step",
                        key=s.id)
                else:
                    add(s.name, s.tool, inherited, "workspace", kind="step", key=s.id)
                continue
            agent = agents.get(s.agent) if s.agent else None
            decision, category = decide(t, resolve_policy(agent, settings))
            add(s.name, s.tool, decision, set_by(agent, category, s.tool), kind="step", key=s.id)
    return gates


#: Steps that call no model as the engine runs them, yet may call one where it cannot be read from the plan: a
#: workflow from elsewhere runs its own steps, and a knowledge base is searched or filled through its embedding model.
_MAY_CALL_A_MODEL = frozenset({"retrieve", "ingest"})


def fills_in(step: Step) -> bool:
    """A tool step with no inputs set on it: a model reads the run so far and fills them in (E1, F967)."""
    return step.type == "tool" and (step.config or {}).get("tool_arguments") is None


def calls_no_model(plan: PlanDraft) -> bool:
    """Whether no step of a run calls a model, its inner workflows' included, read as the engine runs them
    (`step_models.calls_a_model`, F967). A plan with no steps, a step that runs a workflow from elsewhere, or a
    knowledge step is never said to."""
    from core.orchestration.step_models import calls_a_model

    inner = {w.title for w in plan.inner_workflows}
    steps = [s for s in (*plan.steps, *(x for w in plan.inner_workflows for x in w.steps)) if s.type != "end"]
    return bool(steps) and not any(
        s.type in _MAY_CALL_A_MODEL or (s.type == "workflow" and s.workflow not in inner)
        or calls_a_model({"type": s.type, "tool_arguments": (s.config or {}).get("tool_arguments")})
        for s in steps)


async def fill(plan: Plan, server_module: Any, *, found: dict[str, ToolStatus] | None = None,
               agents: dict[str, dict] | None = None) -> Plan:
    """What code knows about a plan, written onto it (a copy): each tool's status, its approvals, what is missing,
    the connections it needs, and whether it fits the workspace's plan.

    * **Tool status** is the run's own lookup (`builder_guard`'s resolver, so "present" means a run could call it);
      a tool research found (`found`) needs its connection first; anything else is missing (F489: it is never
      saved as a step's tool).
    * **Approvals**: every present tool that changes something outside asks first (TAG1's tags, as
      `builder_guard.unapproved` reads them), unless the person already moved it to act without asking — a choice
      kept across fills while the tool is still in the plan. The **gates** are each approval the run will ask for
      and each block, by who asks (`_gates`, F964): the person's choice lifts the build's own confirm only.
    * **What calls a model** (F967): whether no step does, and which tool steps a model fills in.
    * **Plan room** is the host's check (cloud's F154; self-hosted has none).
    """
    from core import builder_guard
    from core.builder_tools import _room_check
    from core.routes.tools import load_custom_tools

    out = plan.model_copy(deep=True)
    exists = await builder_guard._resolver(server_module)
    custom = await load_custom_tools()
    from core.boards import TOOL_NAMES as BOARD_TOOLS

    statuses: list[ToolStatus] = []
    for name in tool_names(out):
        # A board tool is the product's own, never missing nor looked up (F866b): while Boards are off, the review
        # blocks it with their switch (F1203, `build_plan.features_needed`).
        if name in BOARD_TOOLS or await exists(name):
            statuses.append(ToolStatus(name=name, status="present"))
        elif found and name in found:
            # The research's verdict: found (connect first), or missing with why (`none` / `unclear`, A6).
            statuses.append(found[name].model_copy(update={"name": name}))
        else:
            statuses.append(ToolStatus(name=name, status="missing"))
    out.tools = statuses
    out.missing = [t.name for t in statuses if t.status == "missing"]
    # A lead (`api` with no spec yet, via none) has no connection to make until its spec is read (A6).
    out.connections_needed = list(dict.fromkeys(t.provider or t.name for t in statuses
                                                if t.status == "found" and t.via != "none"))

    acting = [t.name for t in statuses if t.status == "present"
              and await builder_guard.acting_tool(t.name, server_module, custom) is not None]
    # An outside service acts until shown otherwise; a tool this build makes that only reads is shown (F1424).
    acting += [t.name for t in statuses if t.status == "found" and not only_reads(t)]
    # A Browser agent holds the browser's tools however it was made (F1305, `gate.held_by`): co_drive, each press it
    # holds asks first too, listed or not; read_only sends nothing at the component's network gate.
    from core.browser.gate import held_by

    for a in out.agents:
        if a.type == "browser" and a.browser is not None and a.browser.mode == "co_drive":
            for t in held_by(a.tools, "browser"):
                if t not in acting and await builder_guard.acting_tool(t, server_module, custom) is not None:
                    acting.append(t)
    kept = set(plan.approvals.acts_without_asking)
    out.gates = await _gates(out, server_module, custom, agents, asks={n for n in acting if n not in kept})
    # The person's "act without asking" lifts this build's own confirm only: what an agent's or the workspace's
    # policy asks still asks, and what it blocks is no approval at all (F964).
    # `asks_first` stays this build's own list, which the compile turns into its new agents' confirms; another's
    # ask is carried in the gates only, so it never spills onto a new agent (SAI2's review, B1).
    held = {g.tool for g in out.gates if g.decision == "confirm" and g.set_by != "build"}
    # Blocked for the plan's own agents only where the workspace blocks it: a reused agent's own block is its own,
    # and never takes a tool from a new agent's confirm (SAI2's re-check, 2).
    denied = {g.tool for g in out.gates if g.decision == "deny" and g.set_by == "workspace"}
    out.approvals = Approvals(asks_first=[n for n in acting if n not in kept and n not in denied],
                              acts_without_asking=[n for n in acting if n in kept and n not in held | denied])

    out.calls_no_model = calls_no_model(out)
    out.model_fills = [s.id for s in out.steps if fills_in(s)]

    # What each part does and what is missing or off, in code's words (F1228): the card's, and the build's last word.
    from core.native_builder import parts_words

    try:
        if agents is None:
            from core.routes.agents import load_user_agents

            agents = {a["id"]: a for a in await load_user_agents() if a.get("id")}
        out.step_words, out.part_words, out.off = parts_words.describe(
            out, titles=await parts_words.tool_titles(server_module), agents=agents,
            kb_names=await parts_words.kb_names())
    except Exception as exc:  # noqa: BLE001 — words it cannot read leave the card as it was; the check goes on
        _log.warning("the plan's parts were not said: %s", exc)

    out.room = None
    if _room_check is not None:
        out.room = (await _room_check("orchestration", 1 + len(out.inner_workflows))
                    or (await _room_check("agent", len(out.agents)) if out.agents else None)) or None
    # The plan's own allowance for what the agents' switches add, as their save checks it (F1223).
    from core import builder_tools

    if out.room is None and (adding := sum(a.memory is True for a in out.agents)):
        out.room = ((await builder_tools._memory_room(adding)) or {}).get("error")
    if out.room is None and any(a.type == "browser" for a in out.agents):
        out.room = ((await builder_tools._browser_refused("agent")) or {}).get("error")
    if out.room is None and out.eval is not None:
        # A host's plan counts its rubrics and golden sets (cloud: Solo 3, Growth 25; F1255).
        from core.product_tools import evals

        out.room = await evals.room(1, 1 if out.eval.cases else 0)
    return out
