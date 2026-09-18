/**
 * Pure graph mutations over an Orchestration.
 *
 * These lived inline in each product's shell (`OrchestrationTab` here,
 * `OrchestrationsClient` in the cloud), which meant the reference-cleanup on
 * delete existed twice and could drift. The canvas also needs them now — the
 * Delete key and edge deletion resolve to the same mutations — so they live in
 * this shared directory, which both products carry verbatim.
 *
 * Everything returns a new object; nothing mutates its input.
 */

import type { Orchestration, StepConfig, StepType } from '@/types/orchestration';

export function generateStepId(): string {
    return 'step_' + Math.random().toString(36).substring(2, 9);
}

export function newStep(type: StepType, position: { x: number; y: number }): StepConfig {
    return {
        id: generateStepId(),
        name: type.charAt(0).toUpperCase() + type.slice(1) + ' Step',
        type,
        position_x: position.x,
        position_y: position.y,
        max_turns: 15,
        timeout_seconds: 300,
        max_iterations: 3,
    };
}

/**
 * Where a step added *without* a drop position lands: to the right of the
 * right-most step, on its row. The old modulo-grid stacked every graph into an
 * unreadable 3-column block; "beside the frontier" at least follows the flow
 * direction, and Tidy up is one click away.
 */
export function findFreePosition(steps: StepConfig[]): { x: number; y: number } {
    if (steps.length === 0) return { x: 80, y: 120 };
    let maxX = -Infinity;
    let yAtMaxX = 120;
    for (const s of steps) {
        const x = s.position_x ?? 0;
        if (x > maxX) {
            maxX = x;
            yAtMaxX = s.position_y ?? 120;
        }
    }
    return { x: maxX + 280, y: yAtMaxX };
}

/**
 * A step whose only way on is `next_step_id` — the kinds that route by a map,
 * a condition, cases or a loop body, and the end, are not. One definition for
 * the panel's Next-step field and the palette's auto-link.
 */
export function isLinearStep(step: StepConfig): boolean {
    return step.type !== 'end' && step.type !== 'evaluator' && step.type !== 'loop' && step.type !== 'if_else' && step.type !== 'switch';
}

/** `step` with `key` added to its input keys — what drawing an edge has always done. */
export function withInputKey(step: StepConfig, key: string | undefined): StepConfig {
    if (!key) return step;
    const existing = step.input_keys || [];
    return existing.includes(key) ? step : { ...step, input_keys: [...existing, key] };
}

/**
 * Append a step; the first step of an empty graph becomes the entry point.
 *
 * With `after` — the step selected when the palette was used — a linear step
 * that has nowhere to go yet goes to the new one, and hands it its output key,
 * as drawing that edge would. Adding steps in a row then builds a chain; before
 * this, four steps added one after another ran as one (F47). A step that
 * already has a next step, or routes some other way, is left alone.
 */
export function addStepToGraph(
    orch: Orchestration,
    type: StepType,
    position?: { x: number; y: number },
    after?: string | null,
): { orchestration: Orchestration; step: StepConfig } {
    const from = after ? orch.steps.find((s) => s.id === after) : undefined;
    const link = !!from && isLinearStep(from) && !from.next_step_id;
    let step = newStep(type, position ?? findFreePosition(orch.steps));
    if (link) step = withInputKey(step, from.output_key);
    const orchestration = {
        ...orch,
        steps: [...orch.steps.map((s) => (link && s.id === from.id ? { ...s, next_step_id: step.id } : s)), step],
        entry_step_id: orch.entry_step_id || step.id,
    };
    return { orchestration, step };
}

/** Remove a step and scrub every reference to it from the remaining steps. */
export function removeStepFromGraph(orch: Orchestration, stepId: string): Orchestration {
    const steps = orch.steps
        .filter((s) => s.id !== stepId)
        .map((s) => {
            const patched: StepConfig = {
                ...s,
                next_step_id: s.next_step_id === stepId ? undefined : s.next_step_id,
                loop_step_ids: s.loop_step_ids?.filter((id) => id !== stepId),
                parallel_branches: s.parallel_branches?.map((branch) => branch.filter((id) => id !== stepId)),
                if_true_step_id: s.if_true_step_id === stepId ? undefined : s.if_true_step_id,
                if_false_step_id: s.if_false_step_id === stepId ? undefined : s.if_false_step_id,
                switch_default_step_id: s.switch_default_step_id === stepId ? undefined : s.switch_default_step_id,
            };
            if (s.route_map) {
                patched.route_map = Object.fromEntries(
                    Object.entries(s.route_map).map(([label, target]) => [label, target === stepId ? null : target]),
                );
            }
            if (s.switch_cases) {
                patched.switch_cases = Object.fromEntries(
                    Object.entries(s.switch_cases).map(([val, target]) => [val, target === stepId ? null : target]),
                );
            }
            return patched;
        });
    return {
        ...orch,
        steps,
        entry_step_id: orch.entry_step_id === stepId ? (steps[0]?.id || '') : orch.entry_step_id,
    };
}

/**
 * The relationship an edge on the canvas stands for — carried on `edge.data`
 * so deleting the edge can clear exactly the field that drew it. `seq` edges
 * (the implicit chains inside a loop body or a parallel branch) are drawn but
 * not deletable: they follow from list membership, not from a link field.
 */
export type EdgeKind =
    | 'next'
    | 'route'
    | 'if_true'
    | 'if_false'
    | 'case'
    | 'switch_default'
    | 'loop_body'
    | 'loop_done'
    | 'parallel_entry'
    | 'seq';

export interface EdgeLink {
    kind: EdgeKind;
    /** route label or switch case value, when kind is 'route' / 'case'. */
    key?: string;
    [key: string]: unknown;
}

/** Clear the link field behind a deleted edge. Unknown kinds are a no-op. */
export function clearEdgeLink(orch: Orchestration, sourceId: string, targetId: string, link: EdgeLink): Orchestration {
    const steps = orch.steps.map((s) => {
        if (s.id !== sourceId) return s;
        switch (link.kind) {
            case 'next':
            case 'loop_done':
                return s.next_step_id === targetId ? { ...s, next_step_id: undefined } : s;
            case 'if_true':
                return { ...s, if_true_step_id: undefined };
            case 'if_false':
                return { ...s, if_false_step_id: undefined };
            case 'switch_default':
                return { ...s, switch_default_step_id: undefined };
            case 'route':
                if (link.key === undefined || !s.route_map) return s;
                return { ...s, route_map: { ...s.route_map, [link.key]: null } };
            case 'case':
                if (link.key === undefined || !s.switch_cases) return s;
                return { ...s, switch_cases: { ...s.switch_cases, [link.key]: null } };
            case 'loop_body':
                return { ...s, loop_step_ids: (s.loop_step_ids || []).filter((id) => id !== targetId) };
            case 'parallel_entry':
                return {
                    ...s,
                    parallel_branches: (s.parallel_branches || []).filter((branch) => branch[0] !== targetId),
                };
            default:
                return s;
        }
    });
    return { ...orch, steps };
}

/** Clone one step (no links, offset position) — the Cmd+D duplicate. */
export function cloneStep(step: StepConfig): StepConfig {
    return {
        ...step,
        id: generateStepId(),
        name: step.name + ' (copy)',
        position_x: (step.position_x ?? 0) + 40,
        position_y: (step.position_y ?? 0) + 40,
        next_step_id: undefined,
        route_map: step.route_map
            ? Object.fromEntries(Object.keys(step.route_map).map((k) => [k, null]))
            : undefined,
        switch_cases: step.switch_cases
            ? Object.fromEntries(Object.keys(step.switch_cases).map((k) => [k, null]))
            : undefined,
        switch_default_step_id: undefined,
        if_true_step_id: undefined,
        if_false_step_id: undefined,
        loop_step_ids: undefined,
        parallel_branches: undefined,
    };
}

/** Fields that only mean something for one type — the type-change guard asks about these. */
const TYPE_SPECIFIC_FIELDS: (keyof StepConfig)[] = [
    'agent_id', 'prompt_template', 'route_map', 'route_descriptions', 'evaluator_prompt',
    'parallel_branches', 'merge_strategy', 'loop_count', 'loop_step_ids',
    'loop_over', 'loop_item_key', 'loop_index_key', 'loop_while_condition',
    'loop_break_condition', 'loop_max_iterations', 'forced_tool',
    'transform_code', 'print_content', 'if_condition', 'if_true_step_id', 'if_false_step_id',
    'switch_expression', 'switch_cases', 'switch_default_step_id',
    'human_prompt', 'human_fields', 'human_channel_id',
];

export function hasTypeSpecificConfig(step: StepConfig): boolean {
    return TYPE_SPECIFIC_FIELDS.some((f) => {
        const v = step[f];
        if (v === undefined || v === null || v === '') return false;
        if (Array.isArray(v)) return v.length > 0;
        if (typeof v === 'object') return Object.keys(v).length > 0;
        return true;
    });
}

/**
 * Keys the engine seeds into shared state on every run, before any step runs:
 * `_init_state()` writes the initial input under both names. They are always
 * valid inputs even though no schema declares them and no step outputs them.
 */
export const BUILTIN_STATE_KEYS = ['user_input', 'user_query'];

/**
 * Every state key the orchestration knows about: the engine's built-ins, the
 * declared schema, plus each step's output key. Feeds the input-key chips and
 * the `state.` helpers.
 */
export function collectStateKeys(orch: Orchestration): string[] {
    const keys = new Set<string>(BUILTIN_STATE_KEYS);
    for (const key of Object.keys(orch.state_schema || {})) keys.add(key);
    for (const s of orch.steps) {
        if (s.output_key) keys.add(s.output_key);
    }
    return [...keys].sort();
}

/**
 * Colors for evaluator routes and switch cases — positional identity for up to
 * six branches (then it cycles). Mid-ramp hexes read on both themes; red is
 * deliberately absent so no route implies an error.
 */
export const ROUTE_COLORS = ['#10b981', '#3b82f6', '#f59e0b', '#8b5cf6', '#ec4899', '#06b6d4'];
