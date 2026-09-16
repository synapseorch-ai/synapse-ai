"""
Pydantic models for the multi-agent orchestration system.
Defines orchestrations (workflow graphs), steps (nodes), and run state.
"""
from typing import Any
from enum import Enum
from pydantic import BaseModel, field_validator, model_validator

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
    human_fields: list[dict[str, str]] = []  # [{name, type, label}]
    human_channel_id: str | None = None      # messaging channel to notify (optional)
    human_timeout_seconds: int = int(ORCH_HUMAN_TIMEOUT)  # how long to wait for messaging response

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
    # Tool cache (always-on for tools in DETERMINISTIC_TOOLS registry; this toggle
    # lets a step opt OUT, e.g. when freshness is critical for a specific run).
    cache_tools_enabled: bool = True
    cache_tool_ttl_seconds: int = 3600

    # On re-invocation (evaluator feedback or loop), include every prior turn's
    # inputs/tools/output in the prompt instead of only the last attempt.
    # Tri-state: True = always include, False = always last-attempt only,
    # None (default) = auto (full history on any re-run).
    include_full_history: bool | None = None

    # Graph routing
    next_step_id: str | None = None  # Linear next step / loop "done" path
    max_iterations: int = 3  # Max times this step can execute in one run (loop guard)

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
    max_total_cost_usd: float | None = None
    timeout_minutes: int = ORCH_GLOBAL_TIMEOUT_MIN

    trigger: str = "manual"  # "manual" | "scheduled"

    #: The rubric every run of this orchestration is scored against
    #: (core/evals). Empty means "not evaluated", which is the opt-in: binding
    #: a rubric is what consents to the judge call's cost. A dangling id — the
    #: rubric was deleted — reads the same as unset rather than failing a run.
    rubric_id: str = ""

    created_at: str | None = None
    updated_at: str | None = None


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
    human_fields: list[dict[str, str]] = []
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
