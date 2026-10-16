"""
Pydantic models for the multi-agent orchestration system.
Defines orchestrations (workflow graphs), steps (nodes), and run state.
"""
from typing import Any
from enum import Enum
from pydantic import BaseModel, PrivateAttr, field_validator, model_validator

from core.config import ORCH_GLOBAL_TIMEOUT_MIN, ORCH_HUMAN_TIMEOUT, ORCH_STEP_TIMEOUT
from core.state_types import normalize_state_type


class StepType(str, Enum):
    AGENT = "agent"
    LLM = "llm"
    TOOL = "tool"
    EVALUATOR = "evaluator"
    PARALLEL = "parallel"
    MERGE = "merge"
    LOOP = "loop"
    HUMAN = "human"
    TRANSFORM = "transform"
    EXTRACT_JSON = "extract_json"
    IF_ELSE = "if_else"
    SWITCH = "switch"
    PRINT = "print"
    END = "end"
    # Knowledge (R1): search knowledge bases, and add records to one.
    RETRIEVE = "retrieve"
    INGEST = "ingest"


class StepConfig(BaseModel):
    """A single step (node) in an orchestration workflow graph."""
    id: str
    name: str
    type: StepType

    # AGENT / EVALUATOR -- which agent to invoke
    agent_id: str | None = None
    prompt_template: str | None = None  # Supports {state.key} references

    # EVALUATOR -- outgoing routes as tools for the agent to call
    # Maps decision label → target step_id (None = end orchestration)
    route_map: dict[str, str | None] = {}
    # Optional description per route — helps LLM understand when to pick each route
    route_descriptions: dict[str, str] = {}
    # Evaluator-specific prompt for the routing decision (separate from agent's prompt_template)
    evaluator_prompt: str | None = None
    # EVALUATOR -- which route to take when the model's reply does not parse.
    #
    # Unset means "the first key of `route_map`", which is only well-defined on
    # a store that preserves object key order. It is not: `definition` is JSONB
    # on Postgres, which sorts keys by (length, bytes). Any evaluator whose
    # fallback matters should name it rather than rely on where a key happens
    # to sort.
    fallback_route: str | None = None
    # Per-step model override (especially useful for evaluators)
    model: str | None = None
    #: A role the host resolves to a model for this step (BWA1, D274): the AI builder's quick steps (its routing
    #: evaluators) are "quick". A run whose state maps the role (`OrchestrationEngine.MODEL_ROLES_KEY`) gives the step
    #: that model, over the run's own; a role the run does not map leaves the step to the run's model, as before.
    model_role: str | None = None

    # PARALLEL -- each inner list is a sequential chain of step IDs
    parallel_branches: list[list[str]] = []

    # MERGE -- how to combine parallel outputs
    merge_strategy: str = "list"  # "list" | "concat" | "dict"

    # LOOP -- run body steps N times, then follow next_step_id (done path)
    loop_count: int = 3
    loop_step_ids: list[str] = []  # ordered steps in loop body
    # LOOP (for-each) -- expression over shared state yielding a list. When set
    # it drives the iteration count instead of loop_count.
    loop_over: str | None = None          # e.g. "state.items"
    loop_item_key: str = "loop_item"      # per-iteration item written here
    loop_index_key: str = "loop_index"    # per-iteration 0-based index written here
    # LOOP (while) -- expression re-evaluated before each iteration
    loop_while_condition: str | None = None
    # LOOP (break) -- expression evaluated after each iteration; True stops the loop
    loop_break_condition: str | None = None
    # Hard ceiling for for-each/while, and the cap loop_count is clamped to
    loop_max_iterations: int = 50

    # TRANSFORM -- Python code to run on shared state
    transform_code: str | None = None

    # PRINT -- user-defined text/markdown stored to output_key
    print_content: str | None = None  # Supports {state.key} interpolation

    # IF_ELSE -- Python condition evaluated against shared state
    if_condition: str | None = None       # e.g. "state.result.flag == True"
    if_true_step_id: str | None = None    # step to go to when condition is True
    if_false_step_id: str | None = None   # step to go to when condition is False

    # SWITCH -- match a state expression against multiple case values
    switch_expression: str | None = None           # e.g. "state.result.status"
    switch_cases: dict[str, str | None] = {}       # {value: target_step_id} (None = end)
    switch_default_step_id: str | None = None      # fallback if no case matches

    # HUMAN -- pause for human input
    human_prompt: str | None = None
    # [{name, type, label}], and a dropdown's `options: [str]` (F455): `core/orchestration/human_fields.py`.
    human_fields: list[dict[str, Any]] = []
    human_channel_id: str | None = None      # messaging channel to notify (optional)
    human_timeout_seconds: int = int(ORCH_HUMAN_TIMEOUT)  # how long to wait for messaging response

    # RETRIEVE / INGEST -- Knowledge's two steps (R1). Each holds the contract's
    # own body, `core.knowledge.api_models.RetrieveStepConfig` /
    # `IngestStepConfig`, as stored: {"kb_ids": [...], "query": "{state.q}", ...}.
    # Held to it when the step runs, not here, so the editor saves a step that is
    # still being filled in (as it saves an agent step with no agent yet) and a
    # bad body never stops an orchestration from loading.
    retrieve: dict[str, Any] | None = None
    ingest: dict[str, Any] | None = None

    # I/O mapping
    # Whether this step's agent sees the attachments the caller sent. Off by
    # default and opt-in per step, because the images are worth real tokens and
    # most steps of most orchestrations have no use for them — on the builder
    # only the analyst and the reporter do.
    #
    # They deliberately do NOT arrive as an `input_keys` entry: those are
    # injected into the prompt as text, and a base64 data URI there would blow
    # the context window and the bill. They ride on the engine instead.
    pass_images: bool = False

    @model_validator(mode="after")
    def _images_only_where_they_are_read(self):
        """`pass_images` is read by the agent executor and nowhere else.

        Accepting it on an `llm` or `tool` step would store a flag that does
        nothing, with no warning — the worst kind of setting, because it looks
        like it worked. Say so at the point it is set instead.
        """
        if self.pass_images and self.type != StepType.AGENT:
            raise ValueError(
                f"pass_images is only read by agent steps; step '{self.id}' is '{self.type.value}'."
            )
        return self

    input_keys: list[str] = []    # Keys to pull from shared state as context
    output_key: str | None = None  # Key to write result into shared state

    # TOOL — forced single tool call with ReAct retry
    forced_tool: str | None = None  # Tool name for TOOL type steps

    # Per-step guardrails
    max_turns: int = 15
    timeout_seconds: int = int(ORCH_STEP_TIMEOUT)
    allowed_tools: list[str] | None = None  # Override agent's tools (narrows only)

    # Response cache (skipped for AGENT steps — state-dependent behaviour)
    cache_responses_enabled: bool = False
    cache_semantic_enabled: bool = False
    cache_response_ttl_seconds: int = 3600
    cache_response_threshold: float = 0.95
    # `cache_tools_enabled` / `cache_tool_ttl_seconds` were here: the tool cache
    # they configured was removed (F67) and the panel's toggle went with batch 3.
    # A document that still carries them validates — unknown keys are ignored.

    # On re-invocation (evaluator feedback or loop), include every prior turn's
    # inputs/tools/output in the prompt instead of only the last attempt.
    # Tri-state: True = always include, False = always last-attempt only,
    # None (default) = auto (full history on any re-run).
    include_full_history: bool | None = None

    # Graph routing
    next_step_id: str | None = None  # Linear next step / loop "done" path
    max_iterations: int = 3  # Max times this step can execute in one run (loop guard)
    # Where a route to this step goes once it has run `max_iterations` times.
    # Unset, the run fails: "'Coder' gave up after 3 attempts" (F248).
    on_exhausted_step_id: str | None = None

    # Visual canvas position (for @xyflow/react)
    position_x: float = 0
    position_y: float = 0


class Orchestration(BaseModel):
    """Top-level orchestration definition -- a workflow graph of steps."""
    id: str
    name: str
    description: str = ""
    avatar: str = "default"
    steps: list[StepConfig] = []
    entry_step_id: str = ""
    state_schema: dict[str, Any] = {}  # {key: {type, default, description}}

    @field_validator("state_schema")
    @classmethod
    def _canonicalise_state_types(cls, schema: Any) -> Any:
        """Store one spelling of `type` — see core.state_types.

        The editor wrote `string`/`number`/`boolean` and the AI builder wrote
        `str`/`float`/`bool` for the same thing. Every save path runs through
        here, so a row saved with either spelling is stored canonically and no
        migration is needed. A type we cannot read is left exactly as it was:
        `_validate_orchestration` has to be able to name it back to the builder,
        and the editor has to be able to show the person what is really stored
        rather than a guess.
        """
        if not isinstance(schema, dict):
            return schema
        out: dict[str, Any] = {}
        for key, entry in schema.items():
            if isinstance(entry, dict) and "type" in entry:
                canonical = normalize_state_type(entry["type"])
                if canonical is not None and canonical != entry["type"]:
                    entry = {**entry, "type": canonical}
            out[key] = entry
        return out

    # Global guardrails
    max_total_turns: int = 100
    # `max_total_cost_usd` was here and read by nothing (F76). A document that
    # still carries it validates: unknown keys are ignored.
    timeout_minutes: int = ORCH_GLOBAL_TIMEOUT_MIN

    #: What one run may spend (CAP1, D272): credits (cloud's; self-hosted has
    #: none) and model dollars. None is no cap, the default. The engine asks
    #: before every step (`core/orchestration/run_cap.py`) — not another F76.
    max_run_credits: float | None = None
    max_run_usd: float | None = None

    #: Offered to MCP clients as a tool of its own, `run_<name>` with its inputs (F466, MCP2, D292). Off by
    #: default: every workflow is a tool otherwise, and a workspace's dozens fill a client's context on every
    #: turn. Off, a client still runs it by id or name through `run_orchestration`.
    offer_to_mcp: bool = False

    @field_validator("max_run_credits", "max_run_usd", mode="before")
    @classmethod
    def _a_cap_or_none(cls, value: Any, info) -> float | None:
        """A stored cap that is not one reads as no cap.

        Every run and resume validates its definition, and a writer that skips
        this model (a bundle, the builder) must not leave a workflow that cannot
        run: a nonsense cap is no cap, never a stop. `OrchestrationIn` refuses
        one where a person saves it.
        """
        from core.orchestration.run_cap import MIN_CREDITS, _positive

        at_least = MIN_CREDITS if info.field_name == "max_run_credits" else None
        return _positive(value, at_least=at_least)

    #: Stop a run when a step's text will not become the type its state key
    #: declares. Off by default, and deliberately: until the engine began
    #: reading `state_schema.type` at all, every type value in every
    #: orchestration was metadata nobody was ever held to — 21% of runs on the
    #: dev stack held something that contradicted one, the AI builder's own
    #: among them. Enforcing unverified annotations by default fails good runs.
    #: Only a failed *text* conversion stops a run: a value that is simply of
    #: another type is a wrong schema, which warns and carries on.
    strict_state_types: bool = False

    trigger: str = "manual"  # "manual" | "scheduled"

    #: The rubric every run of this orchestration is scored against
    #: (core/evals). Empty means "not evaluated", which is the opt-in: binding
    #: a rubric is what consents to the judge call's cost. A dangling id — the
    #: rubric was deleted — reads the same as unset rather than failing a run.
    rubric_id: str = ""

    #: The state key whose value is what a run of this orchestration made (F249):
    #: its chat answer as an agent, v1's `response`, what the judge scores, and
    #: what a parent gets back when this runs nested. Empty means the last step
    #: that produced content — a person's answer, an evaluator's route label and
    #: an End are not what a run made (`core.orchestration.results`).
    result_key: str = ""

    created_at: str | None = None
    updated_at: str | None = None


class OrchestrationIn(Orchestration):
    """An orchestration as a person saves it: a cap that is not one is refused
    (422) with a sentence, where it can be said. Read anywhere else, it is no
    cap (`Orchestration._a_cap_or_none`)."""

    @field_validator("max_run_credits", "max_run_usd", mode="before")
    @classmethod
    def _a_cap_or_none(cls, value: Any, info) -> float | None:
        from core.orchestration.run_cap import clean

        return clean(value, credits=info.field_name == "max_run_credits")


class OrchestrationRun(BaseModel):
    """A single execution instance of an orchestration."""
    run_id: str
    orchestration_id: str
    session_id: str | None = None
    status: str = "running"  # running | paused | completed | failed | cancelled
    shared_state: dict[str, Any] = {}
    step_history: list[dict[str, Any]] = []
    current_step_id: str | None = None
    # The run's task list (core/task_list.py): written through on every
    # accepted update_tasks call, shared across steps, survives a resume.
    tasks: list[dict[str, Any]] = []
    # Mid-step progress by step id — an agent step's transcript after each
    # completed tool turn, so a crash resumes mid-step. Kept apart from
    # shared_state so the per-turn checkpoint writes only this.
    step_progress: dict[str, dict[str, Any]] = {}

    # Human-in-the-loop
    waiting_for_human: bool = False
    human_prompt: str | None = None
    human_fields: list[dict[str, Any]] = []
    # Nested-orchestration human-in-the-loop tracking
    nested_run_id: str | None = None   # sub-run paused waiting for human input
    nested_orch_id: str | None = None  # sub-orchestration definition ID

    # Cost tracking
    total_tokens_used: int = 0
    total_cost_usd: float = 0.0
    # Cache stats (populated by the engine from per-step usage logs)
    cache_read_tokens_total: int = 0
    cache_write_tokens_total: int = 0
    cache_hit_count: int = 0
    cache_miss_count: int = 0
    estimated_savings_usd: float = 0.0

    started_at: str | None = None
    ended_at: str | None = None

    #: The workflow as the run started it (F443): what every answer, tool
    #: approval, nested sub-run and job handed out again carries on with, so an
    #: edit made while the run waits is for the runs after it. A person's
    #: Replay takes the workflow as it is saved then, and this becomes that.
    #: `None` on a run started before the copy was kept. Written once, by the
    #: first full checkpoint after it is set, never by the ones after.
    definition: dict[str, Any] | None = None
    #: Set with `definition` until a checkpoint has stored it.
    _definition_unsaved: bool = PrivateAttr(default=False)

    def keep_definition(self, definition: dict[str, Any]) -> None:
        """Make `definition` the run's copy, for the next full checkpoint to store."""
        self.definition = definition
        self._definition_unsaved = True
