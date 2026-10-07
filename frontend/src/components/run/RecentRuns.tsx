'use client';

/*
 * A short list of runs for Home (M2a; UX1's Prototypes 2–4): a status dot, the
 * workflow and when it started, its path, how long it took and what it cost.
 * Each row opens the run's page.
 *
 * The same row as the Runs list, made compact: no day groups, no columns head,
 * because Home shows the latest few, not every run. With `live` the line under
 * the name says what the run is doing ("Needs input · Approval"), for a list of
 * runs still going.
 */
import Link from 'next/link';
import type { ReactNode } from 'react';

import { cn } from '@/lib/cn';
import type { Orchestration } from '@/types/orchestration';

import { bucketOf, glyphOf, money, type RunRow } from './model';
import { PathGlyph } from './PathGlyph';
import { RUN_DOT, StatusLine } from './RunList';
import { Took, useClock, When } from './When';

export function RecentRuns({ rows, workflows, hrefOf, empty, live = false, label = 'Recent runs' }: {
    rows: RunRow[];
    /** The workflows, for each row's path glyph. */
    workflows: Pick<Orchestration, 'id' | 'steps' | 'entry_step_id'>[];
    hrefOf: (row: RunRow) => string;
    empty: ReactNode;
    /** Runs still going: the line under the name says what each is doing. */
    live?: boolean;
    /** The list's name, for a screen reader. */
    label?: string;
}) {
    const clock = useClock();
    if (!rows.length) return <>{empty}</>;
    const byId = new Map(workflows.map((w) => [w.id, w as Orchestration]));
    const grid = 'grid items-center gap-x-3.5 px-2.5 [grid-template-columns:14px_minmax(0,1fr)_64px] sm:[grid-template-columns:14px_minmax(0,1fr)_auto_64px_72px]';
    return (
        <ul aria-label={label} className="m-0 grid list-none border-t border-border p-0">
            {rows.map((row) => {
                const bucket = bucketOf(row.status, row.waitingForHuman);
                const name = row.workflowName ?? 'Deleted workflow';
                const word = bucket === 'completed' ? 'completed' : bucket === 'needs' ? 'needs input' : bucket;
                return (
                    <li
                        key={row.id}
                        className={cn(
                            grid,
                            'relative min-h-[50px] border-b border-border transition-colors hover:bg-row-hover',
                            'has-[a:focus-visible]:outline has-[a:focus-visible]:outline-2 has-[a:focus-visible]:-outline-offset-2 has-[a:focus-visible]:outline-accent',
                        )}
                    >
                        <span aria-hidden className={cn('inline-block size-2 rounded-full', RUN_DOT[bucket])} />
                        <span className="min-w-0 py-2">
                            <Link
                                href={hrefOf(row)}
                                data-no-focus-ring
                                aria-label={`${name}, ${word}, run ${row.id}`}
                                title={row.id}
                                className='block truncate text-sm font-medium text-text after:absolute after:inset-0 after:content-[""]'
                            >
                                {name}
                            </Link>
                            <span className="block truncate text-xs text-text-faint">
                                {live ? <><StatusLine row={row} /> · </> : null}
                                <When iso={row.startedAt || row.createdAt} clock={clock} />
                            </span>
                        </span>
                        <span className="hidden sm:block">
                            <PathGlyph bars={glyphOf(byId.get(row.workflowId), row)} done={row.stepsDone} />
                        </span>
                        <span className="whitespace-nowrap text-right text-[13px] tabular-nums text-text-muted">
                            <Took start={row.startedAt} end={row.endedAt} />
                        </span>
                        <span className="hidden whitespace-nowrap text-right text-[13px] tabular-nums text-text-muted sm:block" title={row.costHint}>
                            {money(row.costUsd)}
                        </span>
                    </li>
                );
            })}
        </ul>
    );
}
