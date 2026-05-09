"""
Pydantic models for the multi-agent orchestration system.
Defines orchestrations (workflow graphs), steps (nodes), and run state.
"""
from typing import Any
from enum import Enum
from pydantic import BaseModel


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
    # Per-step model override (especially useful for evaluators)
    model: str | None = None

    # PARALLEL -- each inner list is a sequential chain of step IDs
    parallel_branches: list[list[str]] = []

    # MERGE -- how to combine parallel outputs
    merge_strategy: str = "list"  # "list" | "concat" | "dict"

    # LOOP -- run body steps N times, then follow next_step_id (done path)
    loop_count: int = 3
    loop_step_ids: list[str] = []  # ordered steps in loop body

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
    human_timeout_seconds: int = 3600        # how long to wait for messaging response

    # I/O mapping
    input_keys: list[str] = []    # Keys to pull from shared state as context
    output_key: str | None = None  # Key to write result into shared state

    # TOOL — forced single tool call with ReAct retry
    forced_tool: str | None = None  # Tool name for TOOL type steps

    # Per-step guardrails
    max_turns: int = 15
    timeout_seconds: int = 300
    allowed_tools: list[str] | None = None  # Override agent's tools (narrows only)

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

    # Global guardrails
    max_total_turns: int = 100
    max_total_cost_usd: float | None = None
    timeout_minutes: int = 30

    trigger: str = "manual"  # "manual" | "scheduled"
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

    # Human-in-the-loop
    waiting_for_human: bool = False
    human_prompt: str | None = None
    human_fields: list[dict[str, str]] = []

    # Cost tracking
    total_tokens_used: int = 0
    total_cost_usd: float = 0.0

    started_at: str | None = None
    ended_at: str | None = None
