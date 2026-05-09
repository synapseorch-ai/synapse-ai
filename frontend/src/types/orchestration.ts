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

    // I/O mapping
    input_keys?: string[];
    output_key?: string;

    // Per-step guardrails
    max_turns?: number;
    timeout_seconds?: number;
    allowed_tools?: string[];

    // Graph routing
    next_step_id?: string;
    max_iterations?: number;

    // Visual canvas position
    position_x?: number;
    position_y?: number;
}

export interface StateSchemaEntry {
    type: string;
    default: string;
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
    max_total_cost_usd?: number | null;
    timeout_minutes: number;
    trigger: 'manual' | 'scheduled';
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
    started_at?: string;
    ended_at?: string;
}

// xyflow node data
export interface StepNodeData {
    step: StepConfig;
    isEntry: boolean;
    isSelected: boolean;
    agentName?: string;
    runStatus?: 'pending' | 'running' | 'completed' | 'failed';
    [key: string]: unknown;
}

export type StepNode = Node<StepNodeData>;
export type StepEdge = Edge;

// Step type metadata for UI
export const STEP_TYPE_META: Record<StepType, { label: string; color: string; icon: string }> = {
    agent:     { label: 'Agent',     color: '#3b82f6', icon: 'Bot' },
    llm:       { label: 'LLM',       color: '#14b8a6', icon: 'Zap' },
    tool:      { label: 'Tool',      color: '#a855f7', icon: 'Wrench' },
    evaluator: { label: 'Evaluator', color: '#10b981', icon: 'Scale' },
    parallel:  { label: 'Parallel',  color: '#8b5cf6', icon: 'GitBranch' },
    merge:     { label: 'Merge',     color: '#ec4899', icon: 'GitMerge' },
    loop:      { label: 'Loop',      color: '#f59e0b', icon: 'RefreshCw' },
    human:     { label: 'Human',     color: '#ef4444', icon: 'User' },
    transform:    { label: 'Transform',    color: '#6366f1', icon: 'Code' },
    extract_json: { label: 'Extract JSON', color: '#f97316', icon: 'Braces' },
    if_else:      { label: 'If / Else',    color: '#eab308', icon: 'GitFork' },
    switch:       { label: 'Switch',       color: '#06b6d4', icon: 'ArrowLeftRight' },
    print:        { label: 'Print',        color: '#84cc16', icon: 'FileText' },
    end:          { label: 'End',          color: '#6b7280', icon: 'Square' },
};
