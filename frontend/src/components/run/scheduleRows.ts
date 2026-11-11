/*
 * The Schedules page's rows (M2h), pure so both products and their tests share
 * them. A schedule says what it starts, how often (`scheduleWords`), when it
 * runs next and how its last run went — the status only where the product can
 * tell (OSS matches a workflow's runs by their `schedule_<id>` session; cloud's
 * runs carry no schedule, so it knows only when).
 */
import { bucketOf, type RunBucket } from './model';
import { scheduleWords, type ScheduleLike } from './workflowRows';

export type ScheduleListRow = {
    id: string;
    name: string;
    /** Line 2: the description, else the message it sends. */
    sub: string;
    enabled: boolean;
    target: { kind: 'agent' | 'orchestration'; name: string };
    /** "Mondays 07:00 UTC", "Every 2 hours"… */
    words: string;
    /** The expression itself, for the chip's title when the words differ from it. */
    detail?: string;
    nextAt: string | null;
    lastAt: string | null;
    /** The last run's status, when the product could match it. */
    last?: { status: string; waitingForHuman?: boolean; runId: string } | null;
};

/** A schedule as both products return it, mapped to a row. */
export function scheduleRow(
    s: ScheduleLike & {
        description?: string | null;
        prompt?: string | null;
        next_run_at?: string | null;
        last_run_at?: string | null;
    },
    targetName: string,
    last?: ScheduleListRow['last'],
): ScheduleListRow {
    const words = scheduleWords(s);
    const expr = s.schedule_type === 'interval' ? undefined : (s.cron_expression ?? undefined);
    return {
        id: s.id,
        name: s.name || 'Unnamed schedule',
        sub: (s.description || s.prompt || '').trim(),
        enabled: s.enabled !== false,
        target: { kind: s.target_type === 'agent' ? 'agent' : 'orchestration', name: targetName },
        words,
        detail: expr && expr !== words ? expr : undefined,
        nextAt: s.next_run_at ?? null,
        lastAt: s.last_run_at ?? null,
        last: last ?? null,
    };
}

export function lastRunBucket(row: ScheduleListRow): RunBucket | null {
    return row.last ? bucketOf(row.last.status, row.last.waitingForHuman) : null;
}

/** Every word of the query appears in the name, the target, the words, the expression or line 2. */
export function matchSchedule(row: ScheduleListRow, query: string): boolean {
    const words = query.toLowerCase().split(/\s+/).filter(Boolean);
    if (words.length === 0) return true;
    const hay = [row.name, row.target.name, row.words, row.detail, row.sub].filter(Boolean).join(' ').toLowerCase();
    return words.every((w) => hay.includes(w));
}

/** The newest run a schedule started, from a product's runs (newest first), by the `schedule_<id>` session. */
export function lastScheduledRun<T extends { session_id?: string | null }>(scheduleId: string, runs: T[]): T | undefined {
    return runs.find((r) => r.session_id === `schedule_${scheduleId}`);
}
