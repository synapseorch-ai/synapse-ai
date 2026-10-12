/*
 * A run, as both products' run screens read it (M2b).
 *
 * Everything in `components/run/` is copied byte for byte into the cloud app by
 * its `scripts/sync-ui.sh`, like `components/knowledge/`. So nothing here reads
 * an API: each product maps its own responses onto these shapes (OSS in
 * `lib/runHost.tsx`, cloud in its own) and hands them in. This file is the pure
 * part — no React, no fetch — so a list row, the run page and a test all derive
 * the same answer from the same record.
 *
 * What lives here:
 * - the shapes (`RunRow` for a list, `RunRecord` for one run, `NestedRun`)
 * - each step type's role: a model call (cyan), logic (grey) or a person (amber),
 *   the colour rule of D211
 * - a workflow's path order, the order a person reads its steps in
 * - the timeline's rows, from the step history's real starts and durations
 * - the "Followed the saved workflow" line
 * - dates ("Today 7:25 PM"), durations ("1 min 30 s") and money (`$0` when free)
 */
import type { Orchestration, StepConfig, StepType } from '@/types/orchestration';

import { humanFieldType } from '../orchestration/humanFields';

// ── Shapes ───────────────────────────────────────────────────────────────────

/** One row of a run list, whatever product it came from. */
export type RunRow = {
    id: string;
    workflowId: string;
    /** `null` when the workflow has since been deleted. */
    workflowName: string | null;
    status: string;
    waitingForHuman: boolean;
    startedAt: string | null;
    endedAt: string | null;
    /** When the run was asked for; a queued run has no start yet. */
    createdAt?: string | null;
    /** `undefined`: this product's list does not report tokens (the column is left out). */
    tokens?: number | null;
    /** The one cost figure; `null` when nothing prices it. */
    costUsd: number | null;
    /** What the figure leaves out, said on hover (cloud's unpriced calls). */
    costHint?: string;
    /** What started it: "Manual", "Schedule · Weekly review", "Agent chat"… */
    trigger: string;
    currentStepId?: string | null;
    /** The step it is at or stopped at, when the product names it. */
    currentStepName?: string | null;
    /** Steps recorded so far, when the product reports it. */
    stepsDone?: number | null;
    /** The judge's latest verdict (OSS); `null` is "not scored". */
    score?: number | null;
    scoreRegression?: boolean;
    /** Its place in line while it waits for a slot (cloud), else null. */
    queuePosition?: number | null;
    /** Why a run that did not complete stopped, when the product says. */
    error?: string | null;
};

/** One entry of a run's `step_history`, as the engine writes it. */
export type StepExecution = {
    step_id: string;
    step_name?: string;
    step_type?: string;
    status?: string;
    started_at?: string;
    ended_at?: string;
    duration_seconds?: number;
    error?: string | null;
    /** Where in `shared_state` the step put its result. Not the result itself. */
    output_key?: string | null;
};

/**
 * What a step spent (cloud's `step_usage`, by `step_id`). Absent in OSS, whose
 * run record has no per-step spend; the timeline then has no cost column.
 */
export type StepUsage = {
    turns: number;
    models: string[];
    tools: string[];
    tokens: number;
    /** `null` when the step called a model nobody prices. */
    cost_usd: number | null;
    context_peak_chars: number;
    latency_seconds: number;
};

/** One item of the run's working plan (`update_tasks`). */
export type TaskStatus = 'pending' | 'in_progress' | 'completed' | 'cancelled';
export type Task = { content: string; status: TaskStatus };

/** A field a human step asks for — the synced form's own type. */
export type { HumanField } from '../orchestration/humanFields';

/**
 * The one Yes / No field that makes a human step an approval, or `null`: its flag is
 * answered with two buttons, the field's label and Decline (Naveen, M2a).
 */
export function decisionField<F extends { name: string; type?: string }>(fields: F[]): F | null {
    const flags = fields.filter((f) => humanFieldType(f.type) === 'boolean');
    return flags.length === 1 ? flags[0] : null;
}

/** One run, as the run page reads it. */
export type RunRecord = {
    id: string;
    workflowId: string;
    workflowName: string | null;
    status: string;
    waitingForHuman: boolean;
    startedAt: string | null;
    endedAt: string | null;
    createdAt?: string | null;
    currentStepId: string | null;
    tokens: number | null;
    costUsd: number | null;
    costHint?: string;
    trigger?: string | null;
    history: StepExecution[];
    sharedState: Record<string, unknown>;
    tasks: Task[];
    humanPrompt: string | null;
    humanFields: import('../orchestration/humanFields').HumanField[] | null;
    /** What the question is about (OSS sends it), shown above it. */
    humanContext?: string | null;
    /** The state key that context came from, when known: it captions the box (U65). */
    humanContextKey?: string | null;
    /** Why a failed or cancelled run stopped (the engine's `failure_reason`). */
    error: string | null;
    /** Per-step spend, by step id (cloud). */
    stepUsage?: Record<string, StepUsage>;
    /**
     * The workflow as the run started it — the steps it carries on with after a
     * pause, whatever was saved since (F443); after a Replay, what the Replay
     * ran. `null` for a run from before the copy was kept, drawn on today's.
     */
    definition?: Orchestration | null;
};

/** A run a step of this one started (`/runs/{id}/children`). */
export type NestedRun = {
    run_id: string;
    orchestration_id: string;
    orchestration_name?: string | null;
    status: string;
    started_at?: string | null;
    ended_at?: string | null;
    steps_completed: number;
    total_cost_usd?: number | null;
    error?: string | null;
};

// ── Status ───────────────────────────────────────────────────────────────────

/** The statuses the list's pills sort runs into. */
export type RunBucket = 'needs' | 'running' | 'queued' | 'failed' | 'completed' | 'cancelled';

/** A run waiting on a person is the engine's `paused` (or a flag beside `running`). */
export function bucketOf(status: string, waitingForHuman?: boolean): RunBucket {
    if (waitingForHuman || status === 'paused') return 'needs';
    if (status === 'running') return 'running';
    if (status === 'queued') return 'queued';
    if (status === 'completed') return 'completed';
    if (status === 'cancelled') return 'cancelled';
    return 'failed';
}

export const isLive = (status: string) => status === 'running' || status === 'paused' || status === 'queued';

// ── Roles ────────────────────────────────────────────────────────────────────

/** Who does a step's work: a model (cyan), logic (grey) or a person (amber). D211. */
export type StepRole = 'model' | 'logic' | 'person';

/**
 * Each step type's role, by what the engine does in it. A Tool step asks a model
 * for the call's arguments; Retrieve embeds its query. Ingest only queues the
 * records — indexing happens later, outside the run — so it is logic.
 */
const ROLE: Record<StepType, StepRole> = {
    agent: 'model', llm: 'model', tool: 'model', evaluator: 'model', retrieve: 'model',
    human: 'person',
    parallel: 'logic', merge: 'logic', loop: 'logic', if_else: 'logic', switch: 'logic',
    transform: 'logic', extract_json: 'logic', print: 'logic', end: 'logic', ingest: 'logic',
};

export function roleOf(type: string | undefined | null): StepRole {
    return (type && ROLE[type as StepType]) || 'logic';
}

export const ROLE_LABEL: Record<StepRole, string> = {
    model: 'Called a model',
    logic: 'Logic',
    person: 'A person',
};

// ── A workflow's path ────────────────────────────────────────────────────────

type AnyStep = Pick<StepConfig, 'id'> & Partial<StepConfig>;

/** The steps a step leads to, in reading order: inside it first, then onwards. */
function nextOf(step: AnyStep): string[] {
    const out: (string | null | undefined)[] = [];
    for (const branch of step.parallel_branches ?? []) out.push(...branch);
    out.push(...(step.loop_step_ids ?? []));
    out.push(step.if_true_step_id, step.if_false_step_id);
    if (step.switch_cases) out.push(...Object.values(step.switch_cases));
    out.push(step.switch_default_step_id);
    if (step.route_map) out.push(...Object.values(step.route_map));
    out.push(step.next_step_id, step.on_exhausted_step_id);
    return out.filter((id): id is string => typeof id === 'string' && id.length > 0);
}

/**
 * A workflow's steps in the order a person reads them: from the start, each
 * step before what it leads to, a container's body right after the container.
 * Steps nothing reaches come last, in their saved order. Ids only.
 */
export function pathOrder(workflow: Pick<Orchestration, 'steps' | 'entry_step_id'> | null | undefined): string[] {
    if (!workflow) return [];
    const byId = new Map(workflow.steps.map((s) => [s.id, s as AnyStep]));
    const seen = new Set<string>();
    const order: string[] = [];
    const visit = (id: string) => {
        // Iterative, so a long chain cannot overflow the stack.
        const stack = [id];
        while (stack.length) {
            const cur = stack.pop()!;
            if (seen.has(cur) || !byId.has(cur)) continue;
            seen.add(cur);
            order.push(cur);
            const next = nextOf(byId.get(cur)!).filter((n) => !seen.has(n));
            for (let i = next.length - 1; i >= 0; i--) stack.push(next[i]);
        }
    };
    if (workflow.entry_step_id) visit(workflow.entry_step_id);
    for (const s of workflow.steps) if (!seen.has(s.id)) { seen.add(s.id); order.push(s.id); }
    return order;
}

/** How deep each step sits inside containers (a Parallel's branches, a Loop's body). */
export function containerDepths(workflow: Pick<Orchestration, 'steps'> | null | undefined): Map<string, number> {
    const parent = new Map<string, string>();
    for (const s of workflow?.steps ?? []) {
        for (const id of [...(s.parallel_branches ?? []).flat(), ...(s.loop_step_ids ?? [])]) {
            if (!parent.has(id)) parent.set(id, s.id);
        }
    }
    const depth = new Map<string, number>();
    for (const s of workflow?.steps ?? []) {
        let d = 0;
        let cur = parent.get(s.id);
        const guard = new Set<string>();
        while (cur && !guard.has(cur)) { guard.add(cur); d += 1; cur = parent.get(cur); }
        depth.set(s.id, d);
    }
    return depth;
}

/** One bar of a run's path glyph. */
export type GlyphBar = { id: string; role: StepRole; mark?: 'failed' | 'waiting' };

/** The most bars a glyph draws; the rest are counted. */
export const GLYPH_MAX = 12;

/**
 * A list row's path glyph: one bar per step of the workflow, by role. The step
 * a failed run stopped at is red, and the step a waiting run waits at is marked.
 * Nothing else is claimed — a list row does not know which steps ran.
 */
export function glyphOf(workflow: Orchestration | undefined, run: Pick<RunRow, 'status' | 'waitingForHuman' | 'currentStepId'>): GlyphBar[] {
    if (!workflow) return [];
    const byId = new Map(workflow.steps.map((s) => [s.id, s]));
    const bucket = bucketOf(run.status, run.waitingForHuman);
    return pathOrder(workflow)
        .filter((id) => byId.get(id)?.type !== 'end')
        .map((id) => {
            const bar: GlyphBar = { id, role: roleOf(byId.get(id)?.type) };
            if (id === run.currentStepId) {
                if (bucket === 'failed') bar.mark = 'failed';
                else if (bucket === 'needs') bar.mark = 'waiting';
            }
            return bar;
        });
}

// ── The timeline ─────────────────────────────────────────────────────────────

export type TimelineRow = {
    key: string;
    /** The history entry this row draws, by its index; absent for a nested run's row. */
    index?: number;
    stepId: string;
    name: string;
    type: string;
    role: StepRole;
    status: string;
    /** Seconds from the run's start. */
    start: number;
    /** Seconds; `null` while it is still going. */
    duration: number | null;
    depth: number;
    /** A run this step started (an orchestrator agent's workflow). */
    nested?: NestedRun;
    /** Which run of a step this is, when it ran more than once (a loop body). */
    attempt?: number;
};

const toMs = (iso: string | null | undefined): number | null => {
    if (!iso) return null;
    const ms = Date.parse(iso);
    return Number.isNaN(ms) ? null : ms;
};

/** The step id a nested run belongs to: its id is `{parent}__{step}_d{depth}_{hex}`. */
export function nestedStepId(parentRunId: string, childRunId: string): string | null {
    const prefix = `${parentRunId}__`;
    if (!childRunId.startsWith(prefix)) return null;
    const m = /^(.*)_d\d+_[0-9a-f]+$/.exec(childRunId.slice(prefix.length));
    return m ? m[1] : null;
}

/**
 * The timeline: one row per history entry, at its real start and for its real
 * duration, sorted by start; a container before what ran inside it, which is
 * indented; a nested run indented under the step that started it. A step still
 * running (`running`, or the run's current step with no entry yet) is drawn to
 * `now`.
 */
export function timelineRows(
    run: Pick<RunRecord, 'id' | 'startedAt' | 'endedAt' | 'status' | 'currentStepId' | 'history'>,
    workflow: Orchestration | null | undefined,
    children: NestedRun[],
    now: number,
): { rows: TimelineRow[]; span: number; origin: number } {
    const steps = new Map((workflow?.steps ?? []).map((s) => [s.id, s]));
    const depths = containerDepths(workflow);
    const history = run.history;
    const starts = history.map((h) => toMs(h.started_at)).filter((v): v is number => v !== null);
    if (!Number.isFinite(now)) {
        // Before the page mounts there is no "now" both renders agree on: draw a
        // live step to the newest instant the record holds.
        const known = history.flatMap((h) => [toMs(h.started_at), toMs(h.ended_at)]).filter((v): v is number => v !== null);
        now = known.length ? Math.max(...known) : (toMs(run.startedAt) ?? 0);
    }
    const origin = toMs(run.startedAt) ?? (starts.length ? Math.min(...starts) : now);
    const counts = new Map<string, number>();
    for (const h of history) counts.set(h.step_id, (counts.get(h.step_id) ?? 0) + 1);
    const seenTimes = new Map<string, number>();

    const rows: (TimelineRow & { order: number })[] = history.map((h, index) => {
        const begin = toMs(h.started_at) ?? origin;
        const end = toMs(h.ended_at);
        const live = h.status === 'running' || (h.status === 'paused' && !end);
        let duration: number | null;
        if (typeof h.duration_seconds === 'number' && !live) duration = h.duration_seconds;
        else if (end !== null && !live) duration = Math.max(0, (end - begin) / 1000);
        else duration = isLive(run.status) ? null : Math.max(0, ((end ?? begin) - begin) / 1000);
        const step = steps.get(h.step_id);
        const type = h.step_type || step?.type || '';
        const times = (seenTimes.get(h.step_id) ?? 0) + 1;
        seenTimes.set(h.step_id, times);
        return {
            key: `h${index}`,
            index,
            stepId: h.step_id,
            name: h.step_name || step?.name || h.step_id,
            type,
            role: roleOf(type),
            status: h.status || 'completed',
            start: Math.max(0, (begin - origin) / 1000),
            duration,
            depth: depths.get(h.step_id) ?? 0,
            attempt: (counts.get(h.step_id) ?? 0) > 1 ? times : undefined,
            order: index,
        };
    });

    // The step a live run is in has no entry until it ends: draw it from the
    // newest end so far to now.
    if (isLive(run.status) && run.currentStepId && !history.some((h) => h.step_id === run.currentStepId && (h.status === 'running' || h.status === 'paused'))) {
        const ends = history.map((h) => toMs(h.ended_at)).filter((v): v is number => v !== null);
        const begin = ends.length ? Math.max(...ends) : origin;
        const step = steps.get(run.currentStepId);
        const type = step?.type ?? '';
        rows.push({
            key: 'current', stepId: run.currentStepId, name: step?.name || run.currentStepId, type,
            role: roleOf(type), status: run.status === 'paused' ? 'paused' : 'running',
            start: Math.max(0, (begin - origin) / 1000), duration: null,
            depth: depths.get(run.currentStepId) ?? 0, order: history.length,
        });
    }

    for (const child of children) {
        const stepId = nestedStepId(run.id, child.run_id);
        const parentDepth = stepId ? (depths.get(stepId) ?? 0) : 0;
        const begin = toMs(child.started_at) ?? origin;
        const end = toMs(child.ended_at);
        rows.push({
            key: `n:${child.run_id}`, stepId: stepId ?? '', name: child.orchestration_name || child.orchestration_id,
            type: 'nested', role: 'logic', status: child.status,
            start: Math.max(0, (begin - origin) / 1000),
            duration: end !== null ? Math.max(0, (end - begin) / 1000) : (isLive(child.status) ? null : 0),
            depth: parentDepth + 1, nested: child, order: history.length + 1,
        });
    }

    rows.sort((a, b) => (a.start - b.start) || (a.depth - b.depth) || (a.order - b.order));
    // A nested run sits under the step that started it, whatever second it began in.
    const ordered: TimelineRow[] = [];
    const nested = rows.filter((r) => r.nested);
    const placed = new Set<string>();
    for (const r of rows) {
        if (r.nested) continue;
        const { order: _order, ...row } = r;
        void _order;
        ordered.push(row);
        for (const n of nested) {
            if (!placed.has(n.key) && n.stepId === r.stepId && n.start >= r.start - 1) {
                placed.add(n.key);
                const { order: _o, ...child } = n;
                void _o;
                // A nested run's times are whole seconds; it ran inside its step, so it is drawn there.
                const start = Math.max(child.start, r.start);
                const stepEnd = r.duration === null ? null : r.start + r.duration;
                const end = child.duration === null ? null : child.start + child.duration;
                child.start = start;
                child.duration = end === null ? null : Math.max(0, (stepEnd === null ? end : Math.min(end, stepEnd)) - start);
                ordered.push(child);
            }
        }
    }
    for (const n of nested) if (!placed.has(n.key)) { const { order: _o, ...child } = n; void _o; ordered.push(child); }

    const endRun = toMs(run.endedAt);
    const lastEnd = Math.max(0, ...ordered.map((r) => r.start + (r.duration ?? Math.max(0, (now - origin) / 1000 - r.start))));
    const span = Math.max(lastEnd, endRun !== null ? (endRun - origin) / 1000 : 0, 1);
    return { rows: ordered, span, origin };
}

/** Round, readable tick marks for a timeline `span` seconds long (four or five). */
export function ticks(span: number): number[] {
    const steps = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 14400, 43200, 86400];
    const step = steps.find((s) => span / s <= 4) ?? 86400 * Math.ceil(span / 86400 / 4);
    const out: number[] = [];
    for (let t = 0; t <= span + 1e-9; t += step) out.push(t);
    return out;
}

// ── The stepper ──────────────────────────────────────────────────────────────

export type StepState = 'done' | 'failed' | 'waiting' | 'running' | 'cancelled' | 'not-reached';

export type StepperRow = {
    stepId: string;
    name: string;
    type: string;
    role: StepRole;
    state: StepState;
    depth: number;
    /** The newest history entry of this step, when it ran. */
    index?: number;
    /** How many times it ran, past one. */
    times?: number;
};

/**
 * A waiting or failed run as its saved workflow's steps, each with how far the
 * run got: done, failed, waiting, running, cancelled or not reached. End is left
 * out; a step that ran but is no longer in the workflow is added where it ran.
 */
export function stepperRows(
    run: Pick<RunRecord, 'status' | 'waitingForHuman' | 'currentStepId' | 'history'>,
    workflow: Orchestration | null | undefined,
): StepperRow[] {
    const steps = new Map((workflow?.steps ?? []).map((s) => [s.id, s]));
    const depths = containerDepths(workflow);
    const newest = new Map<string, number>();
    const times = new Map<string, number>();
    run.history.forEach((h, i) => { newest.set(h.step_id, i); times.set(h.step_id, (times.get(h.step_id) ?? 0) + 1); });
    const order = workflow ? pathOrder(workflow).filter((id) => steps.get(id)?.type !== 'end') : [];
    for (const h of run.history) if (!order.includes(h.step_id)) order.push(h.step_id);
    const bucket = bucketOf(run.status, run.waitingForHuman);
    return order.map((id) => {
        const step = steps.get(id);
        const i = newest.get(id);
        const entry = i === undefined ? undefined : run.history[i];
        const type = entry?.step_type || step?.type || '';
        let state: StepState = 'not-reached';
        if (entry) {
            state = entry.status === 'failed' ? 'failed'
                : entry.status === 'paused' ? 'waiting'
                    : entry.status === 'running' ? 'running'
                        : entry.status === 'cancelled' ? 'cancelled' : 'done';
        }
        if (id === run.currentStepId && state === 'not-reached') {
            if (bucket === 'needs') state = 'waiting';
            else if (bucket === 'running') state = 'running';
        }
        const n = times.get(id) ?? 0;
        return {
            stepId: id, name: entry?.step_name || step?.name || id, type, role: roleOf(type), state,
            depth: depths.get(id) ?? 0, index: i, times: n > 1 ? n : undefined,
        };
    });
}

// ── Last run, step by step ───────────────────────────────────────────────────

/** How one step went in a run, as the canvas shows it on the step (Last run, M3a-2). */
export type StepTime = {
    state: StepState;
    /** Its time, summed over every time it ran; `null` while it runs or when the run kept none. */
    seconds: number | null;
    /** How many times it ran, past one. */
    times?: number;
    /** What it spent, where the product prices steps (cloud's `stepUsage`); absent in OSS. */
    costUsd?: number | null;
};

/**
 * Each step's state, time and (where the run has it) cost: the state from the
 * stepper's rows, the time from each history entry as the timeline reads it (its
 * `duration_seconds`, else its two timestamps), so the canvas, the run page's
 * stepper and its waterfall can never disagree. An entry that has neither (a
 * question answered while the page watched, before the run's end brings its
 * record) adds nothing rather than a zero. A step the stepper leaves out (End)
 * has no entry: nothing is known about it.
 */
export function stepTimes(
    run: Pick<RunRecord, 'status' | 'waitingForHuman' | 'currentStepId' | 'history' | 'stepUsage'>,
    workflow: Orchestration | null | undefined,
): Record<string, StepTime> {
    const seconds = new Map<string, number>();
    for (const h of run.history) {
        if (h.status === 'running' || h.status === 'paused') continue;
        const begin = toMs(h.started_at);
        const end = toMs(h.ended_at);
        const took = typeof h.duration_seconds === 'number' ? h.duration_seconds
            : begin !== null && end !== null ? Math.max(0, (end - begin) / 1000) : null;
        if (took !== null) seconds.set(h.step_id, (seconds.get(h.step_id) ?? 0) + took);
    }
    const out: Record<string, StepTime> = {};
    for (const row of stepperRows(run, workflow)) {
        const usage = run.stepUsage?.[row.stepId];
        out[row.stepId] = {
            state: row.state,
            seconds: seconds.get(row.stepId) ?? null,
            ...(row.times ? { times: row.times } : null),
            ...(usage ? { costUsd: usage.cost_usd } : null),
        };
    }
    return out;
}

// ── "Followed the saved workflow" ────────────────────────────────────────────

export type Followed = {
    tone: 'success' | 'neutral' | 'person' | 'danger';
    text: string;
    /** A second sentence: the workflow edited since, or not reached, or repeats. */
    notes: string[];
};

/**
 * What the run did against `workflow`, the steps it is drawn on. `null` when
 * there is none to compare with.
 *
 * Given `saved` — the workflow as saved today — `workflow` is the run's own
 * copy (F443): what it ran and, if it waits, what it carries on with. An edit
 * since is said, and what it means for this run. Without it, `workflow` is
 * today's (a run from before the copy was kept), and an edit after the run is
 * said, not hidden.
 */
export function followedLine(
    run: Pick<RunRecord, 'status' | 'waitingForHuman' | 'currentStepId' | 'history' | 'startedAt'>,
    workflow: Orchestration | null | undefined,
    formatWhen: (iso: string) => string,
    saved?: Orchestration | null,
): Followed | null {
    if (!workflow) return null;
    const ownCopy = saved !== undefined;
    const steps = new Map(workflow.steps.map((s) => [s.id, s]));
    const path = pathOrder(workflow).filter((id) => steps.get(id)?.type !== 'end');
    const total = path.length;
    const ran = run.history.filter((h) => h.status !== 'cancelled' || h.ended_at);
    const ranIds = new Set(ran.map((h) => h.step_id));
    const gone = [...new Set(run.history.map((h) => h.step_id))].filter((id) => !steps.has(id) && id);
    const nameOf = (id: string) => steps.get(id)?.name || run.history.find((h) => h.step_id === id)?.step_name || id;
    const notes: string[] = [];

    const edited = (() => {
        const savedAt = ownCopy ? saved?.updated_at : workflow.updated_at;
        const updated = toMs(savedAt);
        const started = toMs(run.startedAt);
        return updated !== null && started !== null && updated > started + 1000 ? savedAt! : null;
    })();
    // Drawn on its own copy, with an edit since: "the saved workflow" is not what it ran.
    const which = ownCopy && edited ? 'the workflow as it started' : 'the saved workflow';

    if (gone.length) notes.push(`${listNames(gone.map(nameOf))} ${gone.length === 1 ? 'is' : 'are'} no longer in the workflow.`);
    const repeats = [...new Set(run.history.map((h) => h.step_id))]
        .map((id) => [id, run.history.filter((h) => h.step_id === id).length] as const)
        .filter(([, n]) => n > 1);
    for (const [id, n] of repeats.slice(0, 3)) notes.push(`${nameOf(id)} ran ${n} times.`);

    const bucket = bucketOf(run.status, run.waitingForHuman);
    const editedNote = (stopped: boolean) => {
        if (!edited) return null;
        if (!ownCopy) return `The workflow was edited after this run (${formatWhen(edited)}).`;
        const said = `The workflow was edited after this run started (${formatWhen(edited)}).`;
        if (bucket === 'needs' || bucket === 'running' || bucket === 'queued') {
            return `${said} This run carries on with the steps it started with; new runs use the edit.`;
        }
        return stopped ? `${said} Shown as this run ran it; Replay carries on with the saved workflow.`
            : `${said} Shown as this run ran it.`;
    };
    const at = (id: string | null | undefined) => {
        const i = id ? path.indexOf(id) : -1;
        return i >= 0 ? `step ${i + 1} of ${total}` : null;
    };
    const failedEntry = [...run.history].reverse().find((h) => h.status === 'failed');
    let tone: Followed['tone'] = 'neutral';
    let text: string;

    if (bucket === 'failed') {
        tone = 'danger';
        const id = failedEntry?.step_id ?? run.currentStepId;
        const where = at(id);
        text = id
            ? `Stopped at ${where ?? 'a step'}${where ? ': ' : ' '}${nameOf(id)}.`
            : 'Stopped before its first step.';
        notes.push(editedNote(true) ?? 'The saved workflow hasn’t changed since.');
        return { tone, text, notes };
    }
    if (bucket === 'needs') {
        tone = 'person';
        const where = at(run.currentStepId);
        text = run.currentStepId ? `Waiting at ${where ?? 'a step'}${where ? ': ' : ' '}${nameOf(run.currentStepId)}.` : 'Waiting for a person.';
    } else if (bucket === 'running' || bucket === 'queued') {
        const where = at(run.currentStepId);
        text = bucket === 'queued' ? 'Waiting for a slot; no step has started.'
            : run.currentStepId ? `At ${where ?? 'a step'}${where ? ': ' : ' '}${nameOf(run.currentStepId)}.` : 'Starting.';
    } else if (bucket === 'cancelled') {
        const last = [...run.history].reverse()[0];
        const where = at(last?.step_id);
        text = last ? `Cancelled at ${where ?? 'a step'}${where ? ': ' : ' '}${nameOf(last.step_id)}.` : 'Cancelled before its first step.';
    } else {
        const reached = path.filter((id) => ranIds.has(id));
        const missed = path.filter((id) => !ranIds.has(id));
        // In order: the first time each step ran follows the path's order.
        const firsts: number[] = [];
        const seen = new Set<string>();
        // A container's entry is written after its body's, at the same start: read it first.
        const depth = containerDepths(workflow);
        const byStart = [...ran].sort((a, b) => ((toMs(a.started_at) ?? 0) - (toMs(b.started_at) ?? 0))
            || ((depth.get(a.step_id) ?? 0) - (depth.get(b.step_id) ?? 0)));
        for (const h of byStart) {
            if (seen.has(h.step_id)) continue;
            seen.add(h.step_id);
            const i = path.indexOf(h.step_id);
            if (i >= 0) firsts.push(i);
        }
        const inOrder = firsts.every((v, i) => i === 0 || v >= firsts[i - 1] || depthBefore(workflow, path, firsts[i - 1], v));
        if (!missed.length && !gone.length) {
            tone = 'success';
            text = `Followed ${which}: ${total} of ${total} steps${inOrder ? ', in order' : ''}.`;
        } else {
            text = `Ran ${reached.length} of ${total} steps of ${which}.`;
            if (missed.length) notes.unshift(`Not reached: ${listNames(missed.map(nameOf), 4)}.`);
        }
    }
    const note = editedNote(bucket === 'cancelled');
    if (note) notes.push(note);
    return { tone, text, notes };
}

/** Parallel branches finish in any order: a later branch starting first is still in order. */
function depthBefore(workflow: Orchestration, path: string[], a: number, b: number): boolean {
    const parallel = workflow.steps.find((s) => (s.parallel_branches ?? []).some((br) => br.includes(path[a])) && (s.parallel_branches ?? []).some((br) => br.includes(path[b])));
    return Boolean(parallel);
}

function listNames(names: string[], max = 3): string {
    const shown = names.slice(0, max);
    const more = names.length - shown.length;
    const head = shown.length > 1 ? `${shown.slice(0, -1).join(', ')} and ${shown[shown.length - 1]}` : shown[0] ?? '';
    return more > 0 ? `${shown.join(', ')} and ${more} more` : head;
}

// ── Words: money, durations, dates ───────────────────────────────────────────

/**
 * A cost: `$0` when free (UX1: `$0.0000` read as a bug), `—` when nothing
 * prices it, four places under a cent and two-to-four above, trailing zeros off.
 */
export function money(usd: number | null | undefined): string {
    if (usd === null || usd === undefined || Number.isNaN(usd)) return '—';
    if (usd === 0) return '$0';
    if (Math.abs(usd) < 0.0001) return '<$0.0001';
    const fixed = usd.toFixed(4).replace(/0+$/, '');
    const [whole, frac = ''] = fixed.split('.');
    return `$${whole}.${frac.padEnd(2, '0')}`;
}

/** "9 s", "1 min 30 s", "2 h 5 min", "1 day". */
export function duration(seconds: number | null | undefined): string {
    if (seconds === null || seconds === undefined || Number.isNaN(seconds)) return '—';
    const s = Math.max(0, seconds);
    if (s < 10) { const r = Math.round(s * 10) / 10; return `${r % 1 === 0 ? r.toFixed(0) : r.toFixed(1)} s`; }
    if (s < 60) return `${Math.round(s)} s`;
    const m = Math.floor(s / 60);
    if (m < 60) { const r = Math.round(s - m * 60); return r ? `${m} min ${r} s` : `${m} min`; }
    const h = Math.floor(m / 60);
    if (h < 24) { const r = m - h * 60; return r ? `${h} h ${r} min` : `${h} h`; }
    const d = Math.floor(h / 24);
    return `${d} ${d === 1 ? 'day' : 'days'}`;
}

/** Seconds between two instants; to `now` when it has not ended. */
export function elapsed(start: string | null | undefined, end: string | null | undefined, now: number): number | null {
    const a = toMs(start);
    if (a === null) return null;
    const b = toMs(end) ?? now;
    return Math.max(0, (b - a) / 1000);
}

/**
 * The instant "today" is read against, and the reader's time zone. Before the
 * page mounts, `now` is NaN and the zone UTC: the server and the browser's first
 * render must print the same text, so dates are absolute until then.
 */
export type Clock = { now: number; timeZone?: string };

export const UNMOUNTED_CLOCK: Clock = { now: Number.NaN, timeZone: 'UTC' };

const settled = (clock: Clock) => Number.isFinite(clock.now);

function parts(ms: number, timeZone?: string) {
    const f = new Intl.DateTimeFormat('en-US', { timeZone, year: 'numeric', month: 'numeric', day: 'numeric' });
    const p = Object.fromEntries(f.formatToParts(new Date(ms)).map((x) => [x.type, x.value]));
    return { y: Number(p.year), m: Number(p.month), d: Number(p.day) };
}

function dayNumber(ms: number, timeZone?: string): number {
    const { y, m, d } = parts(ms, timeZone);
    return Math.round(Date.UTC(y, m - 1, d) / 86_400_000);
}

/** "7:25 PM" (`19:25 UTC` before the page mounts). */
export function timeOfDay(iso: string, clock: Clock): string {
    if (!settled(clock)) return `${new Date(iso).toISOString().slice(11, 16)} UTC`;
    return new Intl.DateTimeFormat(undefined, { timeZone: clock.timeZone, hour: 'numeric', minute: '2-digit' }).format(new Date(iso));
}

/** A day's heading: "Today", "Yesterday", "Monday 5 October", with the year when it isn't this one. */
export function dayLabel(iso: string, clock: Clock): string {
    const ms = toMs(iso);
    if (ms === null) return 'Earlier';
    if (!settled(clock)) {
        return new Intl.DateTimeFormat('en-GB', { timeZone: 'UTC', weekday: 'long', day: 'numeric', month: 'long', year: 'numeric' }).format(new Date(ms));
    }
    const diff = dayNumber(clock.now, clock.timeZone) - dayNumber(ms, clock.timeZone);
    if (diff === 0) return 'Today';
    if (diff === 1) return 'Yesterday';
    const sameYear = parts(ms, clock.timeZone).y === parts(clock.now, clock.timeZone).y;
    return new Intl.DateTimeFormat('en-GB', {
        timeZone: clock.timeZone, weekday: 'long', day: 'numeric', month: 'long', ...(sameYear ? {} : { year: 'numeric' }),
    }).format(new Date(ms));
}

/** The key a run is grouped under: its day, in the reader's time zone. */
export function dayKey(iso: string | null | undefined, clock: Clock): string {
    const ms = toMs(iso);
    return ms === null ? 'none' : String(dayNumber(ms, clock.timeZone));
}

/** "Today 7:25 PM", "Yesterday 3:02 PM", "Mon 5 Oct, 7:25 PM" (UX1's dates). */
export function when(iso: string, clock: Clock): string {
    const ms = toMs(iso);
    if (ms === null) return '—';
    if (!settled(clock)) return `${new Date(ms).toISOString().slice(0, 16).replace('T', ' ')} UTC`;
    const diff = dayNumber(clock.now, clock.timeZone) - dayNumber(ms, clock.timeZone);
    const t = timeOfDay(iso, clock);
    if (diff === 0) return `Today ${t}`;
    if (diff === 1) return `Yesterday ${t}`;
    const sameYear = parts(ms, clock.timeZone).y === parts(clock.now, clock.timeZone).y;
    const day = new Intl.DateTimeFormat('en-GB', {
        timeZone: clock.timeZone, weekday: 'short', day: 'numeric', month: 'short', ...(sameYear ? {} : { year: 'numeric' }),
    }).format(new Date(ms));
    return `${day}, ${t}`;
}

/** The full instant, for a hover: "Tuesday 6 October 2026 at 19:25:54 GMT+5:30". */
export function fullWhen(iso: string, clock: Clock): string {
    const ms = toMs(iso);
    if (ms === null) return '';
    if (!settled(clock)) return new Date(ms).toISOString();
    return new Intl.DateTimeFormat(undefined, {
        timeZone: clock.timeZone, dateStyle: 'full', timeStyle: 'long',
    }).format(new Date(ms));
}

/**
 * A run's trigger from its `session_id`: the scheduler's `schedule_<id>` (OSS,
 * named when the schedule is known) or `schedc_…` (cloud, which keeps no
 * schedule id on the run), none for a run started by hand or through the API,
 * and a chat session's id for a workflow chatted with as an agent.
 */
export function triggerOf(sessionId: string | null | undefined, schedules?: { id: string; name: string }[]): string {
    const session = sessionId || '';
    if (!session) return 'Manual';
    if (session.startsWith('schedule_')) {
        const id = session.slice('schedule_'.length);
        const s = schedules?.find((x) => x.id === id);
        return s ? `Schedule · ${s.name}` : 'Schedule';
    }
    if (session.startsWith('schedc_')) return 'Schedule';
    return 'Agent chat';
}

/** 1st, 2nd, 3rd, 4th … 11th, 12th, 13th, 21st. */
export function ordinal(n: number): string {
    const tens = n % 100;
    if (tens >= 11 && tens <= 13) return `${n}th`;
    return `${n}${({ 1: 'st', 2: 'nd', 3: 'rd' } as Record<number, string>)[n % 10] ?? 'th'}`;
}
