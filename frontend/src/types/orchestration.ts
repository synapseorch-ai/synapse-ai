import type { Node, Edge } from '@xyflow/react';

export type StepType = 'agent' | 'llm' | 'tool' | 'evaluator' | 'parallel' | 'merge' | 'loop' | 'human' | 'transform' | 'extract_json' | 'if_else' | 'switch' | 'print' | 'end';

export interface StepConfig {
    id: string;
    name: string;
    type: StepType;

    // AGENT / EVALUATOR
    agent_id?: string;
    prompt_template?: string;

    // EVALUATOR — outgoing routes as tools for the agent to call
    route_map?: Record<string, string | null>;
    // Optional description per route — helps LLM understand when to pick each
    route_descriptions?: Record<string, string>;
    // Evaluator-specific prompt for the routing decision (separate from agent's prompt_template)
    evaluator_prompt?: string;
    // Per-step model override (for evaluators)
    model?: string;

    // PARALLEL — each inner list is a sequential chain of step IDs
    parallel_branches?: string[][];

    // MERGE — how to combine parallel outputs
    merge_strategy?: 'list' | 'concat' | 'dict';

    // LOOP
    loop_count?: number;
    loop_step_ids?: string[];
    /** Expression yielding a list, e.g. `state.items`. Set = for-each mode. */
    loop_over?: string;
    loop_item_key?: string;
    loop_index_key?: string;
    /** Re-checked before each iteration. Set (without loop_over) = while mode. */
    loop_while_condition?: string;
    /** Checked after each iteration; true stops the loop. */
    loop_break_condition?: string;
    loop_max_iterations?: number;

    // TOOL — forced single tool call
    forced_tool?: string;

    // TRANSFORM
    transform_code?: string;

    // PRINT — user-defined text/markdown with {state.key} interpolation
    print_content?: string;

    // IF_ELSE — Python condition, routes to true/false path
    if_condition?: string;
    if_true_step_id?: string;
    if_false_step_id?: string;

    // SWITCH — match expression against case values
    switch_expression?: string;
    switch_cases?: Record<string, string | null>;
    switch_default_step_id?: string;

    // HUMAN
    human_prompt?: string;
    human_fields?: { name: string; type: string; label: string }[];
    // Optional messaging-channel notification for the pause (first response wins).
    human_channel_id?: string;
    human_timeout_seconds?: number;

    // I/O mapping
    input_keys?: string[];
    output_key?: string;

    // Per-step guardrails
    max_turns?: number;
    timeout_seconds?: number;
    allowed_tools?: string[];

    // Response cache (skipped for AGENT steps)
    cache_responses_enabled?: boolean;
    cache_semantic_enabled?: boolean;
    cache_response_ttl_seconds?: number;
    cache_response_threshold?: number;

    // On re-invocation, include every prior turn (inputs/tools/output) in the prompt.
    include_full_history?: boolean;

    // Graph routing
    next_step_id?: string;
    max_iterations?: number;
    /** Where a route to this step goes once it has used max_iterations. Unset, the run fails (F248). */
    on_exhausted_step_id?: string | null;

    // Visual canvas position
    position_x?: number;
    position_y?: number;
}

export interface StateSchemaEntry {
    type: string;
    default: unknown;
    description: string;
}

export interface Orchestration {
    id: string;
    name: string;
    description: string;
    avatar?: string;
    steps: StepConfig[];
    entry_step_id: string;
    state_schema: Record<string, StateSchemaEntry>;
    max_total_turns: number;
    timeout_minutes: number;
    /** Stop a run when a step's text will not become the type its state key
     *  declares. Off by default — a type nobody was ever held to is not a
     *  thing to start failing runs over. A value that is simply of another
     *  type warns and carries on; only a failed text conversion stops. */
    strict_state_types?: boolean;
    trigger: 'manual' | 'scheduled';
    /** The rubric every run is scored against. Empty means "not evaluated",
     *  which is the opt-in: binding one is what consents to the judge's cost. */
    rubric_id?: string;
    /** The state key whose value is what a run makes: its answer as an agent,
     *  v1's `response`, what a parent gets back when it runs nested, what the
     *  judge scores. Empty means the last step that produced content — a
     *  person's answer, an evaluator's route label and End never count (F249). */
    result_key?: string;
    created_at?: string;
    updated_at?: string;
}

export interface StepExecution {
    step_id: string;
    step_name: string;
    step_type: string;
    status: 'completed' | 'failed';
    started_at?: string;
    ended_at?: string;
    duration_seconds?: number;
    output_key?: string;
    error?: string;
}

export interface OrchestrationRun {
    run_id: string;
    orchestration_id: string;
    status: 'running' | 'paused' | 'completed' | 'failed' | 'cancelled';
    shared_state: Record<string, unknown>;
    step_history: StepExecution[];
    current_step_id?: string;
    waiting_for_human: boolean;
    human_prompt?: string;
    human_fields?: { name: string; type: string; label: string }[];
    total_tokens_used: number;
    total_cost_usd: number;
    cache_read_tokens_total?: number;
    cache_write_tokens_total?: number;
    cache_hit_count?: number;
    cache_miss_count?: number;
    estimated_savings_usd?: number;
    started_at?: string;
    ended_at?: string;
}

// A configuration problem surfaced by `components/orchestration/validate.ts`.
// Errors block a run; warnings are advisory and never block anything.
export interface StepIssue {
    severity: 'error' | 'warning';
    message: string;
}

// xyflow node data
export interface StepNodeData {
    step: StepConfig;
    isEntry: boolean;
    isSelected: boolean;
    agentName?: string;
    runStatus?: 'pending' | 'running' | 'completed' | 'failed';
    /** Why the step failed in the run on screen, in its own words (F195). */
    runError?: string;
    issues?: StepIssue[];
    [key: string]: unknown;
}

export type StepNode = Node<StepNodeData>;
export type StepEdge = Edge;

// Step type metadata for UI. `color` is identity (icon chip, minimap) and is a
// literal hex on purpose — it renders on both themes and never carries meaning
// beyond "which type is this". `category` groups the palette; `blurb` is the
// one-liner shown while choosing a step.
export type StepCategory = 'Core' | 'Logic' | 'Data' | 'I/O';

export const STEP_TYPE_META: Record<StepType, {
    label: string; color: string; icon: string; category: StepCategory; blurb: string;
}> = {
    agent:     { label: 'Agent',     color: '#3b82f6', icon: 'Bot',          category: 'Core',  blurb: 'Run a configured agent with tools' },
    llm:       { label: 'LLM',       color: '#14b8a6', icon: 'Zap',          category: 'Core',  blurb: 'Single LLM call, no tools' },
    tool:      { label: 'Tool',      color: '#a855f7', icon: 'Wrench',       category: 'Core',  blurb: 'Force exactly one tool call' },
    evaluator: { label: 'Evaluator', color: '#10b981', icon: 'Scale',        category: 'Logic', blurb: 'LLM picks the next route' },
    parallel:  { label: 'Parallel',  color: '#8b5cf6', icon: 'GitBranch',    category: 'Logic', blurb: 'Run branches concurrently' },
    merge:     { label: 'Merge',     color: '#ec4899', icon: 'GitMerge',     category: 'Logic', blurb: 'Combine parallel outputs' },
    loop:      { label: 'Loop',      color: '#f59e0b', icon: 'RefreshCw',    category: 'Logic', blurb: 'Repeat body steps N times' },
    human:     { label: 'Human',     color: '#ef4444', icon: 'User',         category: 'I/O',   blurb: 'Pause for a person to decide' },
    transform:    { label: 'Transform',    color: '#6366f1', icon: 'Code',   category: 'Data',  blurb: 'Run Python over shared state' },
    extract_json: { label: 'Extract JSON', color: '#f97316', icon: 'Braces', category: 'Data',  blurb: 'Parse JSON out of text' },
    if_else:      { label: 'If / Else',    color: '#eab308', icon: 'GitFork',       category: 'Logic', blurb: 'Branch on a condition' },
    switch:       { label: 'Switch',       color: '#06b6d4', icon: 'ArrowLeftRight', category: 'Logic', blurb: 'Match a value to a case' },
    print:        { label: 'Print',        color: '#84cc16', icon: 'FileText',      category: 'I/O',   blurb: 'Write text/markdown to state' },
    end:          { label: 'End',          color: '#6b7280', icon: 'Square',        category: 'I/O',   blurb: 'Terminate the flow here' },
};
