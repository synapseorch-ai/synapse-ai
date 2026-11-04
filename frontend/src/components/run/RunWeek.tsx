'use client';

/*
 * The week at a glance (M2a; UX1's Home): one bar per run of the last seven
 * days, oldest first, coloured by how it ended — a quiet green for done, red
 * for a failure, amber for a run waiting on a person, cyan for one at work.
 * A finished run's height says how long it took; the others stand full height.
 *
 * The bars are a picture, not a list to tab through: a screen reader gets the
 * week in one sentence, and a pointer can open a bar's run. Every run is a row
 * in Recent runs and on the Runs page for the keyboard.
 */
import Link from 'next/link';

import { cn } from '@/lib/cn';

import { barHeights, WEEK_BAR, WEEK_MAX } from './home';
import { DELETED_WORKFLOW, bucketOf, when, type Clock, type RunRow } from './model';

const WORD = { completed: 'completed', failed: 'failed', needs: 'waiting on a person', running: 'running', queued: 'queued', cancelled: 'cancelled' } as const;

const LEGEND: { cls: string; label: string }[] = [
    { cls: WEEK_BAR.completed, label: 'Completed' },
    { cls: WEEK_BAR.failed, label: 'Failed' },
    { cls: WEEK_BAR.needs, label: 'Waiting' },
    { cls: WEEK_BAR.running, label: 'Running' },
];

export function RunWeek({ rows, hrefOf, clock, total }: {
    /** The week's runs, oldest first (`weekRuns`). */
    rows: RunRow[];
    hrefOf: (row: RunRow) => string;
    clock: Clock;
    /** How many ran this week, when the rows are only the latest of them (cloud reads 60). */
    total?: number;
}) {
    const shown = rows.slice(-WEEK_MAX);
    const all = Math.max(total ?? rows.length, rows.length);
    if (!shown.length) return <p className="text-sm text-text-faint">No runs this week.</p>;
    const heights = barHeights(shown);
    const counts = shown.reduce<Record<string, number>>((c, r) => {
        const b = bucketOf(r.status, r.waitingForHuman);
        c[b] = (c[b] ?? 0) + 1;
        return c;
    }, {});
    const said = (['completed', 'failed', 'needs', 'running', 'queued', 'cancelled'] as const)
        .filter((b) => counts[b])
        .map((b) => `${counts[b]} ${WORD[b]}`)
        .join(', ');
    return (
        // `relative`: the sentence for a screen reader is absolutely placed, and without a positioned
        // ancestor it would be placed against the document and stretch it (ui-smoke's `layout`).
        <div className="relative grid gap-2">
            <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1 text-xs text-text-faint">
                <span>{shown.length === all ? `Last ${shown.length} ${shown.length === 1 ? 'run' : 'runs'}` : `The latest ${shown.length} of ${all} runs`}, oldest first</span>
                <span aria-hidden className="flex flex-wrap items-center gap-x-3 gap-y-1">
                    {LEGEND.map((l) => (
                        <span key={l.label} className="inline-flex items-center gap-1.5">
                            <i className={cn('inline-block h-2 w-2 rounded-[2px]', l.cls)} />
                            {l.label}
                        </span>
                    ))}
                </span>
            </div>
            <p className="sr-only">This week, {shown.length === all ? '' : `the latest ${shown.length} of `}{all} {all === 1 ? 'run' : 'runs'}: {said}.</p>
            <div
                aria-hidden
                className="grid h-10 items-end gap-[3px]"
                style={{ gridTemplateColumns: `repeat(${Math.max(shown.length, 36)}, minmax(0, 1fr))` }}
            >
                {shown.map((r, i) => {
                    const b = bucketOf(r.status, r.waitingForHuman);
                    const at = r.startedAt || r.createdAt;
                    return (
                        <Link
                            key={r.id}
                            href={hrefOf(r)}
                            tabIndex={-1}
                            title={`${r.workflowName ?? DELETED_WORKFLOW} · ${WORD[b]}${at ? ` · ${when(at, clock)}` : ''}`}
                            suppressHydrationWarning
                            className={cn('block min-w-[3px] rounded-[2px_2px_1px_1px] transition-opacity hover:opacity-80', WEEK_BAR[b])}
                            style={{ height: `${heights[i]}%` }}
                        />
                    );
                })}
            </div>
        </div>
    );
}
