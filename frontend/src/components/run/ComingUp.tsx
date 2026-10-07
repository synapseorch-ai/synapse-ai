'use client';

/*
 * What runs next (M2a; UX1's Home): the next scheduled runs, soonest first —
 * a day box, the schedule's name, the time and what it runs.
 */
import { dayBox } from './home';
import { timeOfDay, type Clock } from './model';

export type Upcoming = {
    id: string;
    /** The schedule's name. */
    name: string;
    /** Its next run (ISO). */
    at: string;
    /** What it runs: the workflow's or the agent's name. */
    target?: string | null;
};

export function ComingUp({ items, clock }: { items: Upcoming[]; clock: Clock }) {
    if (!items.length) return <p className="text-sm text-text-faint">Nothing scheduled.</p>;
    return (
        <ul aria-label="Coming up" className="m-0 grid list-none border-t border-border p-0">
            {items.map((u) => {
                const box = dayBox(u.at, clock);
                return (
                    <li key={u.id} className="grid grid-cols-[40px_minmax(0,1fr)] items-center gap-3 border-b border-border py-3">
                        <span className="text-center text-[11px] font-semibold uppercase leading-tight text-text-faint" suppressHydrationWarning>
                            {box.weekday}
                            <b className="block text-lg font-semibold leading-tight text-text" suppressHydrationWarning>{box.day}</b>
                        </span>
                        <span className="min-w-0">
                            <span className="block truncate text-sm font-semibold text-text">{u.name}</span>
                            <span className="block truncate text-[12.5px] text-text-faint" suppressHydrationWarning>
                                {timeOfDay(u.at, clock)}{u.target ? ` · ${u.target}` : ''}
                            </span>
                        </span>
                    </li>
                );
            })}
        </ul>
    );
}

/** The next `n` enabled schedules by their next run. */
export function upcoming<S extends { id: string; name?: string; enabled?: boolean; next_run_at?: string | null; target_type?: string; target_id?: string }>(
    schedules: S[],
    names: (s: S) => string | null,
    n = 4,
): Upcoming[] {
    return schedules
        .filter((s) => s.enabled !== false && s.next_run_at)
        .sort((a, b) => Date.parse(a.next_run_at as string) - Date.parse(b.next_run_at as string))
        .slice(0, n)
        .map((s) => ({ id: s.id, name: s.name || 'Unnamed schedule', at: s.next_run_at as string, target: names(s) }));
}
