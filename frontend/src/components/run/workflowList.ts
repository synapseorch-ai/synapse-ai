/*
 * The Workflows list's words and order (M2k), pure so both products and their
 * tests share them: what starts a workflow (its trigger chip), its last run, and
 * the sorts the list offers. `WorkflowList` draws the rows.
 *
 * Schedules run in UTC in both products (the engine's `compute_next_run`), so a
 * schedule's time says so.
 */
import { bucketOf, type RunBucket } from './model';
import { templatePath } from './StepPath';

/** A schedule as both products' `/schedules` return it (only what the words need). */
export type ScheduleLike = {
    id: string;
    name?: string | null;
    enabled?: boolean | null;
    target_type?: string | null;
    target_id?: string | null;
    schedule_type?: string | null;
    interval_value?: number | null;
    interval_unit?: string | null;
    cron_expression?: string | null;
};

const DAYS = ['Sundays', 'Mondays', 'Tuesdays', 'Wednesdays', 'Thursdays', 'Fridays', 'Saturdays'];

function ordinalDay(n: number): string {
    const v = n % 100;
    const s = ['th', 'st', 'nd', 'rd'][(v - 20) % 10] || ['th', 'st', 'nd', 'rd'][v] || 'th';
    return `${n}${s}`;
}

/**
 * A cron expression in a few words: "Mondays 07:00 UTC", "Weekdays 09:00 UTC",
 * "Daily 06:30 UTC", "Hourly at :15", "Monthly, 1st 08:00 UTC". Anything else
 * comes back as the expression itself.
 */
export function cronWords(expr: string | null | undefined): string {
    const raw = (expr ?? '').trim();
    const parts = raw.split(/\s+/);
    if (parts.length !== 5) return raw;
    const [min, hour, dom, mon, dow] = parts;
    const isNum = (s: string) => /^\d+$/.test(s);
    if (mon !== '*') return raw;
    if (isNum(min) && hour === '*' && dom === '*' && dow === '*') {
        return `Hourly at :${min.padStart(2, '0')}`;
    }
    if (!isNum(min) || !isNum(hour)) return raw;
    const time = `${hour.padStart(2, '0')}:${min.padStart(2, '0')} UTC`;
    if (dom === '*' && dow === '*') return `Daily ${time}`;
    if (dom === '*') {
        if (dow === '1-5') return `Weekdays ${time}`;
        if (dow === '0,6' || dow === '6,0' || dow === '6-7') return `Weekends ${time}`;
        if (isNum(dow) && Number(dow) <= 7) return `${DAYS[Number(dow) % 7]} ${time}`;
        return raw;
    }
    if (dow === '*' && isNum(dom)) return `Monthly, ${ordinalDay(Number(dom))} ${time}`;
    return raw;
}

/** A schedule's rhythm in a few words: an interval ("Every 2 hours") or its cron's. */
export function scheduleWords(s: ScheduleLike): string {
    if (s.schedule_type === 'interval') {
        const n = s.interval_value ?? 1;
        const unit = (s.interval_unit ?? 'minutes').replace(/s$/, '');
        return n === 1 ? `Every ${unit}` : `Every ${n} ${unit}s`;
    }
    return cronWords(s.cron_expression);
}

export type TriggerChip = {
    kind: 'manual' | 'schedule';
    /** What the chip says: "Mondays 07:00 UTC", "2 schedules", "Scheduled", "Manual". */
    label: string;
    /** The schedules' names and rhythms, for the chip's title. */
    detail?: string;
};

/**
 * What starts a workflow, never an empty pill (UX1): its one enabled schedule's
 * rhythm, a count of several, "Scheduled" when it says so but no schedule of it
 * was found, and "Manual" otherwise.
 */
export function triggerChip(
    workflow: { id: string; trigger?: string | null },
    schedules: ScheduleLike[] | undefined,
): TriggerChip {
    const own = (schedules ?? []).filter(
        (s) => s.target_type === 'orchestration' && s.target_id === workflow.id && s.enabled !== false,
    );
    const detail = own.map((s) => `${s.name || 'Unnamed schedule'}: ${scheduleWords(s)}`).join('\n') || undefined;
    if (own.length === 1) return { kind: 'schedule', label: scheduleWords(own[0]), detail };
    if (own.length > 1) return { kind: 'schedule', label: `${own.length} schedules`, detail };
    if (workflow.trigger === 'scheduled') return { kind: 'schedule', label: 'Scheduled' };
    return { kind: 'manual', label: 'Manual' };
}

export type WorkflowLastRun = {
    id: string;
    status: string;
    waitingForHuman?: boolean;
    /** When it started (or was created, before it started). */
    at: string | null;
    startedAt?: string | null;
    endedAt?: string | null;
    /** `undefined` when the product doesn't know it (cloud's runs older than the list's read). */
    costUsd?: number | null;
    costHint?: string;
};

export type WorkflowListRow = {
    id: string;
    name: string;
    description: string;
    /** The steps' types in reading order, End dropped (`templatePath`). */
    types: string[];
    hasPerson: boolean;
    stepCount: number;
    trigger: TriggerChip;
    lastRun: WorkflowLastRun | null;
    /** Live runs right now (cloud counts them; OSS leaves it out). */
    runningCount?: number;
    updatedAt?: string | null;
};

type WorkflowLike = {
    id: string;
    name: string;
    description?: string | null;
    steps: { id: string; type?: string | null }[];
    entry_step_id?: string | null;
    trigger?: string | null;
    updated_at?: string | null;
};

/** One row of the list from a product's workflow, its last run and the schedules. */
export function workflowRow(
    workflow: WorkflowLike,
    lastRun: WorkflowLastRun | null,
    schedules: ScheduleLike[] | undefined,
    runningCount?: number,
): WorkflowListRow {
    const { types, hasPerson } = templatePath(workflow);
    return {
        id: workflow.id,
        name: workflow.name,
        description: workflow.description ?? '',
        types,
        hasPerson,
        // The steps the path draws (End is a marker, not work), so the count and the tiles agree.
        stepCount: types.length,
        trigger: triggerChip(workflow, schedules),
        lastRun,
        runningCount,
        updatedAt: workflow.updated_at ?? null,
    };
}

/** The last run's bucket, or null when it never ran. */
export function lastBucket(row: WorkflowListRow): RunBucket | null {
    return row.lastRun ? bucketOf(row.lastRun.status, row.lastRun.waitingForHuman) : null;
}

export type WorkflowSort =
    | 'updated-new' | 'updated-old' | 'name-az' | 'name-za' | 'steps-most' | 'steps-least'
    | 'last-urgent' | 'last-calm' | 'trigger-az' | 'trigger-za';

/** Every sort cloud's table had (its five columns, both ways), as one menu. */
export const WORKFLOW_SORTS: { value: WorkflowSort; label: string }[] = [
    { value: 'updated-new', label: 'Updated, newest first' },
    { value: 'updated-old', label: 'Updated, oldest first' },
    { value: 'name-az', label: 'Name, A to Z' },
    { value: 'name-za', label: 'Name, Z to A' },
    { value: 'steps-most', label: 'Most steps' },
    { value: 'steps-least', label: 'Fewest steps' },
    { value: 'last-urgent', label: 'Last run, needs you first' },
    { value: 'last-calm', label: 'Last run, never run first' },
    { value: 'trigger-az', label: 'Trigger, A to Z' },
    { value: 'trigger-za', label: 'Trigger, Z to A' },
];

/**
 * Cloud's urgency order for the last run: a paused run first (it waits on
 * someone), then failed, running, queued, completed, cancelled; never run last.
 */
export function urgencyOf(row: WorkflowListRow): number {
    const r = row.lastRun;
    if (!r) return 9;
    if (r.status === 'paused') return 0;
    return ({ failed: 1, running: 2, queued: 3, completed: 4, cancelled: 5 } as Record<string, number>)[r.status] ?? 6;
}

export function sortWorkflows(rows: WorkflowListRow[], sort: WorkflowSort): WorkflowListRow[] {
    const out = [...rows];
    const by = <T,>(key: (r: WorkflowListRow) => T, dir: 1 | -1) =>
        out.sort((a, b) => {
            const x = key(a), y = key(b);
            return x < y ? -dir : x > y ? dir : 0;
        });
    const name = (r: WorkflowListRow) => r.name.toLowerCase();
    const updated = (r: WorkflowListRow) => r.updatedAt ?? '';
    switch (sort) {
        case 'updated-new': return by(updated, -1);
        case 'updated-old': return by(updated, 1);
        case 'name-az': return by(name, 1);
        case 'name-za': return by(name, -1);
        case 'steps-most': return by((r) => r.stepCount, -1);
        case 'steps-least': return by((r) => r.stepCount, 1);
        case 'last-urgent': return by(urgencyOf, 1);
        case 'last-calm': return by(urgencyOf, -1);
        case 'trigger-az': return by((r) => r.trigger.label.toLowerCase(), 1);
        case 'trigger-za': return by((r) => r.trigger.label.toLowerCase(), -1);
    }
}

/** Whether a row matches the search: its name or description. */
export function matchWorkflow(row: WorkflowListRow, query: string): boolean {
    const q = query.trim().toLowerCase();
    if (!q) return true;
    return row.name.toLowerCase().includes(q) || row.description.toLowerCase().includes(q);
}
