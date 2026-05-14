"""
Native Builder — Synapse's AI Builder implemented as a real orchestration.

This package holds the seed JSON definitions (orchestration + sub-agents) and
the idempotent seeding function used at startup. See seed.py.
"""
from .seed import seed_native_builder, NATIVE_BUILDER_ORCH_ID, NATIVE_BUILDER_AGENT_ID

__all__ = [
    "seed_native_builder",
    "NATIVE_BUILDER_ORCH_ID",
    "NATIVE_BUILDER_AGENT_ID",
    "STEP_TYPE_CHEATSHEET",
]


# Shared step-type reference used by the plan drafter prompt AND the saver
# agents' system prompts so the drafter's plan and the saver's materialised
# JSON agree on the step palette.
#
# Fields listed per type are the ones that MUST be populated for that type
# (beyond the common id/name/type/input_keys/output_key/next_step_id/
# max_turns/timeout_seconds/max_iterations). Unlisted fields may be left at
# their zero-defaults (`route_map: {}`, `parallel_branches: []`, etc.) so the
# engine deserialises cleanly.
STEP_TYPE_CHEATSHEET = """\
- **agent**: runs a configured sub-agent with a prompt + its tool set. Required: `agent_id`, `prompt_template`. Optional: `include_full_history` (bool) — on re-invocation show every prior turn's inputs/tools/output instead of only the last attempt; useful for feedback/retry loops. Use for any step that needs multi-turn reasoning or tool use.
- **llm**: single one-shot LLM call, no tools. Required: `prompt_template` (optional `model`). Use for lightweight summarisation, rewriting, or deterministic prose generation.
- **tool**: forces a single tool call with no LLM reasoning. Required: `forced_tool` (+ `agent_id` for tool-resolution). Use when the arguments are already in state and just need forwarding.
- **evaluator**: pure routing node. Required: `route_map_json` (JSON-encoded `{label: target_step_id}`), `route_descriptions_json` (JSON), `evaluator_prompt`. Output_key stores the bare route label. Use to fork on a classifier decision.
- **parallel**: runs multiple branches concurrently. Required: `parallel_branches_json` (JSON-encoded list of step-id lists), `next_step_id` (where they converge). Use when independent work can overlap.
- **merge**: combines parallel-branch outputs. Required: `merge_strategy` (`concat` | `list` | `dict`). Place immediately after a parallel's convergence.
- **loop**: runs a body N times. Required: `loop_step_ids` (ordered body), `loop_count`. Use for fixed repetition; for conditional loops use an evaluator that routes back.
- **human**: pauses for user input. Required: `human_prompt`, `human_fields_json` (JSON-encoded list of `{name, type, label}`). Output_key stores the response dict.
- **transform**: runs a snippet of Python against state. Required: `transform_code` — reads `state`, assigns to `result`. Use for pure-data reshapes; avoid heavy logic.
- **end**: terminates the orchestration. No type-specific fields. Every flow must reach an `end` step.

JSON-string fields (the saver's tool schemas accept these as JSON-encoded strings — Gemini hangs on open objects, so these are passed as strings and parsed server-side):
- `route_map_json` → object mapping decision label → target step_id. Empty string ends that branch. Example: `'{"approved":"step_abc1234","denied":""}'`
- `route_descriptions_json` → object mapping label → short classifier hint. Example: `'{"approved":"User accepts the draft"}'`
- `parallel_branches_json` → array of arrays of step ids. Example: `'[["step_a","step_b"],["step_c"]]'`
- `human_fields_json` → array of `{name, type, label}` objects. Example: `'[{"name":"approved","type":"boolean","label":"Approve?"}]'`
- `state_schema_json` (orchestration-level) → object mapping key → `{type, default, description}`. Allowed types: str, int, float, bool, list, dict. Example: `'{"user_input":{"type":"str","default":"","description":"Initial message"}}'`

Wiring rules that apply to every step:
- `id` format: `step_` + 7 lowercase-alphanumeric chars, unique within the orchestration.
- `output_key` of an upstream step must appear in the `input_keys` of any downstream step that reads it.
- `entry_step_id` at the orchestration level must match the first step actually executed.
- Agents are referenced by their real `agent_xxxxxxx` ID — never invent one.
- For tool steps, confirm the tool exists via `get_tools_detail` before naming it in `forced_tool`.

State lifecycle rules (CRITICAL — think of state keys as step outputs):
- `user_input` and `user_query` are ALWAYS pre-populated with the user's initial message before any step runs. Use either freely in `input_keys` or `{state.user_input}` / `{state.user_query}` in `prompt_template`.
- Every other state key enters state via exactly one mechanism: some step's `output_key` writes it. Keys that no step writes are meaningless — they can only ever be their schema default.
- HARD RULE: a key may appear in a step's `input_keys` (or be referenced as `{state.X}` in a prompt) only if ONE of these holds:
  (a) X is `user_input` or `user_query`, OR
  (b) Some step in the orchestration has `output_key: X`.
- Branch conditioning is NOT the validator's concern. If a key's producer sits on one branch and a downstream step on another branch still reads it, that's fine — the engine falls back to the schema default on unset reads. Iterative refinement loops (first pass reads the default, subsequent passes read real data) are first-class patterns.
- When designing `state_schema`, for each declared key you must be able to name the single step whose `output_key` writes it. If you cannot name the writer, delete the key — don't declare or read phantom keys.
"""
