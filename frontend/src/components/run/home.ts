/*
 * Home's numbers and words (M2a; UX1's approval-first Home), pure, so both
 * products' Homes and a test derive the same answer from the same rows.
 *
 * Like the rest of `components/run/`, nothing here reads an API: OSS counts its
 * run list in the browser, cloud reads its summary on the server, and each
 * hands the figures in.
 */
import { bucketOf, type Clock, type RunBucket, type RunRow } from './model';

const DAY_MS = 86_400_000;

/**
 * How many waiting runs Home answers in place before the rest become rows. Here, not in
 * `NeedsYou`, so a server page can read it: a client module's exports reach a server
 * component as references, not values.
 */
export const MAX_CARDS = 5;

/** How many runs the week's bars draw at most. */
export const WEEK_MAX = 60;

const startMs = (r: Pick<RunRow, 'startedAt' | 'createdAt'>) => {
    const iso = r.startedAt || r.createdAt;
    const ms = iso ? Date.parse(iso) : NaN;
    return Number.isNaN(ms) ? null : ms;
};

/** The runs that started in the seven days before `now`, oldest first. */
export function weekRuns<R extends Pick<RunRow, 'startedAt' | 'createdAt'>>(rows: R[], now: number): R[] {
    return rows
        .filter((r) => {
            const ms = startMs(r);
            return ms !== null && ms > now - 7 * DAY_MS && ms <= now + 60_000;
        })
        .sort((a, b) => (startMs(a) ?? 0) - (startMs(b) ?? 0));
}

export type WeekStats = {
    runs: number;
    completed: number;
    failed: number;
    /** The newest failure of the week, for the strip's link. */
    lastFailure: RunRow | null;
};

/** Counts over a week's runs (from `weekRuns`). */
export function weekStats(week: RunRow[]): WeekStats {
    let completed = 0, failed = 0;
    let lastFailure: RunRow | null = null;
    for (const r of week) {
        const b = bucketOf(r.status, r.waitingForHuman);
        if (b === 'completed') completed += 1;
        if (b === 'failed') { failed += 1; lastFailure = r; }
    }
    return { runs: week.length, completed, failed, lastFailure };
}

/** "91.7%", as cloud's summary rounds it; `—` when nothing ran. */
export function rate(completed: number, runs: number): string {
    if (!runs) return '—';
    const r = Math.round((completed / runs) * 1000) / 10;
    return `${r}%`;
}

const SMALL = ['No', 'One', 'Two', 'Three', 'Four', 'Five', 'Six', 'Seven', 'Eight', 'Nine'];
const counted = (n: number) => (n < SMALL.length ? SMALL[n] : String(n));

/**
 * Home's one status line, under the greeting: what needs the reader, then how
 * the rest of the week went.
 */
export function statusLine({ waiting, runs, failed }: { waiting: number; runs: number; failed: number }): string {
    const first = waiting === 0
        ? 'Nothing needs you.'
        : `${counted(waiting)} ${waiting === 1 ? 'thing needs' : 'things need'} you.`;
    let second: string;
    if (failed > 0) second = `${counted(failed)} ${failed === 1 ? 'run' : 'runs'} failed this week.`;
    else if (runs === 0) second = waiting ? '' : 'Nothing has run this week.';
    else second = waiting ? 'Everything else ran as planned.' : 'Everything ran as planned.';
    return second ? `${first} ${second}` : first;
}

/** "Good evening, Riley" in the reader's own time; `null` until the page has mounted. */
export function greeting(clock: Clock, name: string | null | undefined): string | null {
    if (!Number.isFinite(clock.now)) return null;
    const hour = Number(new Intl.DateTimeFormat('en-US', { timeZone: clock.timeZone, hour: 'numeric', hourCycle: 'h23' }).format(new Date(clock.now)));
    const part = hour < 5 ? 'evening' : hour < 12 ? 'morning' : hour < 18 ? 'afternoon' : 'evening';
    const who = name?.trim();
    return who ? `Good ${part}, ${who}` : `Good ${part}`;
}

/** "Tuesday 6 October", the reader's today; `null` until mounted. */
export function today(clock: Clock): string | null {
    if (!Number.isFinite(clock.now)) return null;
    return new Intl.DateTimeFormat('en-GB', { timeZone: clock.timeZone, weekday: 'long', day: 'numeric', month: 'long' }).format(new Date(clock.now));
}

/** A person's first name from one free-text name ("Riley Morgan" → "Riley"). */
export function firstName(name: string | null | undefined): string | null {
    const first = (name ?? '').trim().split(/\s+/)[0];
    return first || null;
}

/** "Mon" and "12": a day's box in Coming up, in the reader's zone (UTC until mounted). */
export function dayBox(iso: string, clock: Clock): { weekday: string; day: string } {
    const zone = Number.isFinite(clock.now) ? clock.timeZone : 'UTC';
    const d = new Date(iso);
    return {
        weekday: new Intl.DateTimeFormat('en-GB', { timeZone: zone, weekday: 'short' }).format(d),
        day: new Intl.DateTimeFormat('en-GB', { timeZone: zone, day: 'numeric' }).format(d),
    };
}

/** What a week bar is: the outcome's colour class, and whether its height says how long it took. */
export const WEEK_BAR: Record<RunBucket, string> = {
    completed: 'bg-[color-mix(in_oklab,var(--success)_45%,var(--surface-2))]',
    failed: 'bg-danger',
    needs: 'bg-person',
    running: 'bg-machine',
    queued: 'bg-border-strong',
    cancelled: 'bg-border-strong',
};

/** A bar's height in percent: a finished run by how long it took (square root, so one slow run does not flatten the rest). */
export function barHeights(rows: RunRow[]): number[] {
    const secs = rows.map((r) => {
        const a = r.startedAt ? Date.parse(r.startedAt) : NaN;
        const b = r.endedAt ? Date.parse(r.endedAt) : NaN;
        return Number.isNaN(a) || Number.isNaN(b) ? null : Math.max(0, (b - a) / 1000);
    });
    const max = Math.max(1, ...secs.map((s) => (s === null ? 0 : Math.sqrt(s))));
    return rows.map((r, i) => {
        const b = bucketOf(r.status, r.waitingForHuman);
        const s = secs[i];
        if (b !== 'completed' || s === null) return 100;
        return Math.round(30 + 70 * (Math.sqrt(s) / max));
    });
}
