"""What of a run's or a chat's events a client may see — one list, for every door (F854, BF11, D311).

The engine yields events for two readers. Its own: the loggers, the resume
snapshots, the publishers' bookkeeping — `_log_llm_call` carries an agent's
whole system prompt and assembled prompt, `_step_progress` its transcript. And
a client's: the chat panel, the run page, the API, MCP. Until F854 each door
decided for itself — the widget and v1 chat by an allowlist, the builder by
dropping `_` events, a run stream by dropping `_log_` events — and the chat
stream decided nothing, so QA1c read an agent's prompt, with its secret
variables rendered, in the browser.

`public_event` is the one decision. A type it does not know is dropped, not
passed: a new event reaches a client only once someone has said it may, and
`tests/unit/test_every_event_is_public_or_internal.py` makes every type the
engine yields say which it is. Secrets are scrubbed by name as it passes
(`core.secret_scrub`), and the engine's own state keys are taken out of the
state a finished run hands over.

Narrower doors keep their own lists on top of this one: the widget's visitor
events, v1 chat's frames, the OpenAI endpoint's chunks.
"""
from __future__ import annotations

from typing import Any

#: What a client follows: the chat panel, the run page, the builder, a team's and a helper's lanes, the API.
PUBLIC_EVENT_TYPES = frozenset({
    # A turn.
    "status", "thinking", "llm_reasoning", "llm_thought", "reasoning_only",
    "tool_call", "tool_call_unparsed", "tool_execution", "tool_result", "tool_approval_replay",
    "tool_approval_resolved", "truncated_output", "repeat_stopped",
    "tasks_updated", "tasks_incomplete", "team_tasks", "skill_loaded",
    "context_compact", "context_trimmed",
    "answer_delta", "answer_reset", "final", "response", "turn_usage", "agent_step_result",
    "knowledge_search", "knowledge_read", "knowledge_citations",
    "human_input_required", "step_warning",
    "queued", "worker_picked_up", "paused", "cancelled", "error", "done",
    # A failed turn's one offer: the same message again on a stronger model, at its price (F1018).
    "try_stronger", "effort_offer",
    # An agent at its turn limit that went on by itself (F1219, `turn_limit_mode` continue): N more turns.
    "turn_limit_extended",
    # The browser inside Synapse (BRW1): a session opened, and what each agent action did (no page text).
    "browser_session", "browser_action",
    # Helpers and team members, each wrapping a narrowed event of its own (`helpers.SHOWN`, `team.SHOWN`).
    "helper_started", "helper_event", "helper_finished", "helper_results_read", "helper_result_kept",
    "member_started", "member_event", "member_finished", "member_answered",
    # A run.
    "orchestration_start", "orchestration_complete", "orchestration_end", "orchestration_error",
    "step_start", "step_complete", "step_error", "step_progress",
    "evaluator_decision", "routing_decision", "if_decision", "switch_decision",
    "parallel_start", "branch_start", "parallel_complete", "merge_complete",
    "loop_iteration", "loop_complete", "loop_limit_reached", "transform_result",
    "knowledge_retrieve", "knowledge_ingest",
    "journal_truncated", "run_stream_end", "stream_complete",
    # The builder.
    "resource_saved", "builder_notice", "connection_needed", "action_needed", "research_findings",
    # "Open that chat": the person's own Synapse AI chat holding a workflow this one may not write (TABS2, F1063).
    "open_chat",
})

#: The engine's own: read inside the process, before any door, and never shown.
INTERNAL_EVENT_TYPES = frozenset({
    "_log_llm_call", "_log_prompt", "_log_evaluator", "_log_tool_step_llm",
    "_step_progress", "_inbox_parked", "_model_delta", "_model_restart",
    # Why a build stopped (F768): the engine keeps it in the run's record (`run_writes.note_stopped`) and the summary
    # says it; no client reads the event itself.
    "build_stopped",
})

#: A door's own frames, built from public events after its own narrowing: v1 chat's
#: (`session`, and `awaiting_approval` from `api_v1._pause_of`) and the OpenAI
#: endpoint's. They share a chat turn's log with the engine's events
#: (`core.chat_turns`), which keeps them as they are.
DOOR_FRAME_TYPES = frozenset({"session", "awaiting_approval", "answer", "delta", "reset", "usage", "waiting"})

#: State keys the engine keeps for itself — progress records, routing decisions,
#: loop accumulators, execution memory. Named explicitly rather than "anything
#: starting with _": a user's own state_schema key may start with an
#: underscore, and that one is theirs to read back.
PRIVATE_STATE_PREFIXES = (
    "_step_progress_", "_loop_progress_", "_parallel_progress_", "_exec_memory_",
    "_routing_", "_loop_", "_if_decision_", "_switch_decision_",
    # A delegate's step's tasks until its entry carries them (TEAM1d, `pauses.TASKS_KEY`).
    "_step_tasks",
    # Whom an assistant run works for (ASSIST1): the host's, never shown.
    "_caller",
    # The browser's own keys in a run (F1205): the sessions its start picked (`picks.RUN_KEY`), and any the browser
    # keeps there later, by its prefix.
    "_browser_",
    # Which models a run's steps use (`OrchestrationEngine.MODEL_OVERRIDE_KEY`, `MODEL_ROLES_KEY`).
    "_model_override", "_model_roles",
    # Where a waiting human step was announced (`human_prompts.HUMAN_CHANNEL_KEY`).
    "_human_channel_id",
    # A build's record of what it wrote, what failed and why it stopped (`run_writes`); its words are `run_changes`.
    "_run_writes", "_run_failures", "_run_stopped",
    # Synapse AI's approved review and its send-backs (`review.REVIEW_KEY`, `build_plan._SENDBACK_KEY`), and the
    # structure findings a build has said already (`build_plan._STRUCTURE_SAID_KEY`, D340 D3).
    "_approved_review", "_plan_sendbacks", "_plan_structure_said",
    # That a save refused after the yes went back to the planner once (`build_save.SAVE_REPLAN_KEY`, F1295).
    "_save_replanned",
    # That a change request's words went back once for a card (`change_tools.WORDS_SENT_KEY`, F1310).
    "_change_words_sent",
    # The person's own API specs a conversation read, by where they are (`own_api.SPECS_KEY`, F1298).
    "_specs_read",
    # Whether a person watches the run, which decides what a step does at its turn limit (`turn_limit.ATTENDED_KEY`,
    # F1219): the engine's, set by its starter, never a run's input or a client's to read.
    "_attended",
    # The calls a step's reply made after its finish tool, which were not made (`steps.NOT_MADE_PREFIX`, F1242); and
    # the kinds a change named its new things as, within its step (`change_tools._KINDS_KEY`).
    "_not_made_", "_change_kinds",
)


def public_state(state: Any) -> Any:
    """A run's shared state without the engine's own keys."""
    if not isinstance(state, dict):
        return state
    return {k: v for k, v in state.items() if not (isinstance(k, str) and k.startswith(PRIVATE_STATE_PREFIXES))}


def public_event(event: Any, *, also: frozenset[str] = frozenset()) -> dict | None:
    """`event` as a client may see it, or None when it is not one a client sees.

    `also` names an embedder's own public types (cloud's), which pass the same way.
    """
    from core.secret_scrub import scrub_secrets_in

    if not isinstance(event, dict):
        return None
    kind = event.get("type")
    if kind not in PUBLIC_EVENT_TYPES and kind not in also:
        return None
    shown = dict(event)
    # A finished run hands over its state: the engine's keys stay with the engine.
    if "final_state" in shown:
        shown["final_state"] = public_state(shown["final_state"])
    if isinstance(shown.get("data"), dict) and "shared_state" in shown["data"]:
        shown["data"] = {**shown["data"], "shared_state": public_state(shown["data"]["shared_state"])}
    return scrub_secrets_in(shown)
