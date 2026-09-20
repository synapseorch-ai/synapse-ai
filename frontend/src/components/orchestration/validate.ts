/**
 * Static checks over an orchestration draft.
 *
 * Pure and synchronous so the canvas can badge nodes on every edit and the
 * shells can gate Run without a round-trip. Severity is the contract:
 * an `error` is something the engine will trip over (a dangling target, an
 * agent step with no agent); a `warning` is something that probably isn't what
 * the author meant (an unreachable step, an unused input key). Errors block
 * Run; nothing blocks Save — a half-built draft must always be saveable.
 */

import type { Orchestration, StepIssue } from '@/types/orchestration';
import { BUILTIN_STATE_KEYS } from './graph';

export interface ValidationResult {
    byStep: Record<string, StepIssue[]>;
    global: StepIssue[];
    errorCount: number;
    warningCount: number;
}

const error = (message: string): StepIssue => ({ severity: 'error', message });
const warning = (message: string): StepIssue => ({ severity: 'warning', message });

/**
 * `agents` is the list the caller already holds for the picker. When it is
 * given, an `agent_id` that names nothing in it is an error — the engine
 * refuses to run such a step, and before it did it silently ran whichever
 * agent sorted first, which is worse than any error. When it is omitted (a
 * caller with no agent list), that check is skipped rather than guessed at.
 */
export function validateOrchestration(orch: Orchestration, agents?: { id: string }[]): ValidationResult {
    const byStep: Record<string, StepIssue[]> = {};
    const global: StepIssue[] = [];
    const ids = new Set(orch.steps.map((s) => s.id));
    const agentIds = agents ? new Set(agents.map((a) => a.id)) : null;
    const add = (stepId: string, issue: StepIssue) => {
        (byStep[stepId] ??= []).push(issue);
    };
    const checkTarget = (stepId: string, target: string | null | undefined, what: string) => {
        if (target && !ids.has(target)) add(stepId, error(`${what} points at a step that no longer exists`));
        if (target === stepId) add(stepId, error(`${what} points at the step itself`));
    };

    // --- Graph level ---
    if (orch.steps.length === 0) {
        global.push(warning('No steps yet — add one from the palette'));
    } else if (!orch.entry_step_id) {
        global.push(error('No entry point set'));
    } else if (!ids.has(orch.entry_step_id)) {
        global.push(error('Entry point references a deleted step'));
    }

    // --- Known state keys for input_keys checks: the engine's built-ins
    // (user_input/user_query are seeded on every run), the schema, and every
    // output key. Flagging a built-in taught this check its first false alarm.
    const knownKeys = new Set(BUILTIN_STATE_KEYS);
    for (const key of Object.keys(orch.state_schema || {})) knownKeys.add(key);
    for (const s of orch.steps) if (s.output_key) knownKeys.add(s.output_key);

    // --- Duplicate output keys ---
    const outputOwners = new Map<string, string[]>();
    for (const s of orch.steps) {
        if (s.output_key) outputOwners.set(s.output_key, [...(outputOwners.get(s.output_key) || []), s.id]);
    }
    for (const [key, owners] of outputOwners) {
        if (owners.length > 1) {
            for (const id of owners) add(id, warning(`Output key “${key}” is written by ${owners.length} steps — later runs overwrite earlier ones`));
        }
    }

    // --- Per step ---
    for (const s of orch.steps) {
        switch (s.type) {
            case 'agent':
                if (!s.agent_id) add(s.id, error('No agent selected'));
                break;
            case 'llm':
                if (!s.prompt_template?.trim()) add(s.id, error('No prompt set'));
                break;
            case 'tool':
                if (!s.forced_tool) add(s.id, error('No tool selected'));
                break;
            case 'evaluator': {
                const routes = Object.entries(s.route_map || {});
                if (routes.length === 0) add(s.id, warning('No routes — the evaluator has nothing to choose between'));
                for (const [label, target] of routes) checkTarget(s.id, target, `Route “${label}”`);
                break;
            }
            case 'if_else':
                if (!s.if_condition?.trim()) add(s.id, error('No condition set'));
                checkTarget(s.id, s.if_true_step_id, 'True path');
                checkTarget(s.id, s.if_false_step_id, 'False path');
                break;
            case 'switch': {
                const cases = Object.entries(s.switch_cases || {});
                if (!s.switch_expression?.trim()) add(s.id, error('No expression set'));
                if (cases.length === 0) add(s.id, warning('No cases — everything falls through to the default path'));
                for (const [val, target] of cases) checkTarget(s.id, target, `Case “${val}”`);
                checkTarget(s.id, s.switch_default_step_id, 'Default path');
                break;
            }
            case 'loop': {
                const body = s.loop_step_ids || [];
                if (body.length === 0) add(s.id, warning('Loop body is empty'));
                for (const id of body) {
                    if (!id) add(s.id, warning('Loop body has an unassigned slot'));
                    else if (!ids.has(id)) add(s.id, error('Loop body references a step that no longer exists'));
                }
                if (body.includes(s.id)) add(s.id, error('Loop body contains the loop itself — it would never end'));

                const cap = s.loop_max_iterations ?? 50;
                if (cap < 1) add(s.id, error('Max iterations must be at least 1'));
                if (!s.loop_over && !s.loop_while_condition) {
                    const count = s.loop_count ?? 3;
                    if (count < 1) add(s.id, error('Iteration count must be at least 1'));
                    else if (count > cap) add(s.id, warning(`Iteration count (${count}) is above the ${cap}-iteration cap and will be clamped`));
                }
                if (s.loop_over && s.loop_while_condition) {
                    add(s.id, warning('Both a collection and a while condition are set — the collection wins'));
                }

                const bodySet = new Set(body);
                for (const id of body) {
                    const b = orch.steps.find((x) => x.id === id);
                    if (!b) continue;
                    // The loop runs its body as an ordered list; _resolve_next never
                    // runs for these, so a branch decided in here goes nowhere.
                    if (b.type === 'if_else' || b.type === 'switch' || b.type === 'evaluator') {
                        add(s.id, warning(`“${b.name}” routes, but routing inside a loop body is ignored — put the branch after the loop`));
                    }
                    // A parallel branch auto-follows next_step_id; a loop body does not.
                    if (b.next_step_id && !bodySet.has(b.next_step_id) && b.next_step_id !== s.next_step_id) {
                        add(s.id, warning(`“${b.name}” points on to a step outside the loop body, which the loop will not run`));
                    }
                }
                break;
            }
            case 'parallel': {
                const branches = s.parallel_branches || [];
                if (branches.length === 0) add(s.id, warning('No branches — nothing runs in parallel'));
                for (const branch of branches) {
                    if (branch.length === 0) add(s.id, warning('A branch has no entry step'));
                    else checkTarget(s.id, branch[0], 'A branch entry');
                }
                // The join. A branch listed by its entry step follows that step's
                // next-step chain until it reaches this step's own next step — so
                // with none set, every branch runs the whole rest of the flow, one
                // branch after another, and the first to reach an End step takes
                // the run with it. An error, not a warning: the run misbehaves.
                if (!s.next_step_id) {
                    add(s.id, error('No next step — set it to the Merge step where the branches meet, or each branch runs the rest of the flow'));
                } else {
                    const join = orch.steps.find((x) => x.id === s.next_step_id);
                    if (join && join.type !== 'merge') {
                        add(s.id, warning(`Branches meet at “${join.name}”, which is not a Merge step, so what they produced is not combined`));
                    }
                }
                break;
            }
            case 'transform':
                if (!s.transform_code?.trim()) add(s.id, error('No Python code set'));
                break;
            case 'human':
                if (!s.human_prompt?.trim()) add(s.id, warning('No prompt for the human — they will see an empty question'));
                break;
            case 'print':
                if (!s.print_content?.trim()) add(s.id, warning('No content set'));
                break;
            case 'extract_json':
                if (!s.input_keys?.length) add(s.id, warning('No input keys — nothing to extract from'));
                break;
        }

        // Any step may name an agent (agent steps must, evaluators may). A name
        // that resolves to nothing is an error wherever it appears.
        if (s.agent_id && agentIds && !agentIds.has(s.agent_id)) {
            add(s.id, error('The selected agent no longer exists — pick another'));
        }

        checkTarget(s.id, s.next_step_id, 'Next step');

        for (const key of s.input_keys || []) {
            if (!knownKeys.has(key)) {
                add(s.id, warning(`Input key “${key}” is not in the state schema and no step outputs it`));
            }
        }
    }

    // --- Reachability from the entry point ---
    if (orch.entry_step_id && ids.has(orch.entry_step_id) && orch.steps.length > 1) {
        const reachable = new Set<string>();
        const queue = [orch.entry_step_id];
        const stepById = new Map(orch.steps.map((s) => [s.id, s]));
        while (queue.length) {
            const id = queue.pop()!;
            if (reachable.has(id)) continue;
            reachable.add(id);
            const s = stepById.get(id);
            if (!s) continue;
            const targets: (string | null | undefined)[] = [
                s.next_step_id, s.if_true_step_id, s.if_false_step_id, s.switch_default_step_id,
                ...Object.values(s.route_map || {}),
                ...Object.values(s.switch_cases || {}),
                ...(s.loop_step_ids || []),
                ...(s.parallel_branches || []).flat(),
            ];
            for (const t of targets) if (t && ids.has(t)) queue.push(t);
        }
        for (const s of orch.steps) {
            if (!reachable.has(s.id)) add(s.id, warning('Unreachable from the entry point'));
        }
    }

    let errorCount = global.filter((i) => i.severity === 'error').length;
    let warningCount = global.filter((i) => i.severity === 'warning').length;
    for (const issues of Object.values(byStep)) {
        for (const i of issues) {
            if (i.severity === 'error') errorCount++;
            else warningCount++;
        }
    }
    return { byStep, global, errorCount, warningCount };
}
