'use client';

/*
 * The runs nested directly under this one (F251; cloud's `NestedRuns`, moved
 * upstream in M2b).
 *
 * A step that runs another workflow starts a run of its own, with its own id
 * (`{run}__{step}_d{depth}_{hex}`). The run lists show roots only, so this is
 * where a nested run is found: under the run that started it, one row per run
 * of the nested step, with its status, how far it got, what it cost and why it
 * failed. Each row opens its own run. The timeline above also draws each one
 * under the step that started it.
 */
import Link from 'next/link';

import { cn } from '@/lib/cn';

import { useRunHost } from './host';
import { money, type NestedRun } from './model';

const DOT: Record<string, string> = {
    completed: 'bg-success',
    failed: 'bg-danger',
    cancelled: 'bg-text-faint',
    paused: 'bg-person',
    queued: 'bg-text-faint',
};

export function NestedRuns({ runs }: { runs: NestedRun[] }) {
    const host = useRunHost();
    if (runs.length === 0) return null;
    return (
        <section aria-label="Nested runs" className="rounded-md border border-border bg-surface px-3 py-2">
            <h4 className="mb-1 text-2xs font-medium uppercase tracking-wider text-text-faint">
                Nested runs ({runs.length})
            </h4>
            <ul className="space-y-0.5">
                {runs.map((child) => (
                    <li key={child.run_id}>
                        <Link
                            href={host.links.run(child.run_id)}
                            className="flex items-baseline gap-2 rounded px-1 py-0.5 text-xs transition-colors hover:bg-surface-2"
                        >
                            <span aria-hidden className={cn('size-1.5 shrink-0 rounded-full', DOT[child.status] ?? 'bg-machine motion-safe:animate-pulse')} />
                            <span className="truncate text-text">{child.orchestration_name || child.orchestration_id}</span>
                            <span className="shrink-0 capitalize text-text-muted">{child.status}</span>
                            <span className="shrink-0 text-text-faint">
                                {child.steps_completed} step{child.steps_completed === 1 ? '' : 's'}
                            </span>
                            {child.total_cost_usd ? (
                                <span className="shrink-0 tabular-nums text-text-faint">{money(child.total_cost_usd)}</span>
                            ) : null}
                            {child.error && (
                                <span className="min-w-0 truncate text-danger" title={child.error}>{child.error}</span>
                            )}
                        </Link>
                    </li>
                ))}
            </ul>
        </section>
    );
}
