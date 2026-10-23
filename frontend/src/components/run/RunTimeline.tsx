'use client';

/*
 * The run as a path: a waterfall of real starts and durations (UX1, M2b).
 *
 * One row per step the run executed, at the second it started and for as long
 * as it took, from the step history the engine writes — nothing estimated.
 * Cyan bars called a model, amber waited on a person, grey is logic, red
 * failed. A step inside a Parallel or a Loop is indented under its container;
 * a run a step started (an agent that runs a workflow) is indented under that
 * step and opens its own page. A row with something to read opens in place:
 * the step's output, its error, and (cloud) what it spent.
 */
import { ChevronRight } from 'lucide-react';
import Link from 'next/link';
import { useMemo, useState } from 'react';

import { cn } from '@/lib/cn';
import type { Orchestration } from '@/types/orchestration';

import { useRunHost } from './host';
import {
    creditsText, entrySpend, duration, isLive, money, ordinal, ROLE_LABEL, ticks, timelineRows,
    type NestedRun, type RunRecord, type StepRole, type TimelineRow,
} from './model';
import { StepDetail, hasDetail } from './StepDetail';
import { typeLabel } from './StepIcon';
import { useClock } from './When';

const BAR: Record<StepRole, string> = {
    model: 'bg-machine',
    logic: 'bg-border-strong',
    person: 'bg-person',
};

export function RunTimeline({ run, workflow, nested }: {
    run: RunRecord;
    workflow: Orchestration | null;
    nested: NestedRun[];
}) {
    const host = useRunHost();
    const live = isLive(run.status);
    const clock = useClock(live ? 1000 : 60_000);
    const { rows, span } = useMemo(
        () => timelineRows(run, workflow, nested, clock.now),
        [run, workflow, nested, clock.now],
    );
    const [open, setOpen] = useState<Set<string>>(new Set());
    const withCost = Boolean(run.stepUsage);
    const marks = ticks(span);
    const cols = withCost
        ? '[grid-template-columns:minmax(120px,1fr)_minmax(0,1.2fr)_56px] sm:[grid-template-columns:minmax(170px,1.1fr)_minmax(0,2fr)_76px]'
        : '[grid-template-columns:minmax(120px,1fr)_minmax(0,1.2fr)] sm:[grid-template-columns:minmax(170px,1.1fr)_minmax(0,2fr)]';
    const roles = new Set(rows.filter((r) => !r.nested).map((r) => r.role));
    const pct = (s: number) => `${Math.min(100, Math.max(0, (s / span) * 100)).toFixed(2)}%`;

    if (rows.length === 0) {
        return <p className="text-sm text-text-muted">{live ? 'No step has finished yet. Activity below follows what it is doing now.' : 'This run recorded no steps.'}</p>;
    }

    const toggle = (key: string) => setOpen((cur) => {
        const next = new Set(cur);
        if (next.has(key)) next.delete(key); else next.add(key);
        return next;
    });

    const gridLines = (
        <span aria-hidden className="pointer-events-none absolute inset-0">
            {marks.slice(1).map((t) => (
                <span key={t} className="absolute inset-y-0 w-px bg-border" style={{ left: pct(t) }} />
            ))}
        </span>
    );

    return (
        <div className="grid gap-3">
            <div role="table" aria-label="Steps on a timeline" className="grid">
                <div role="row" className={cn('grid items-center gap-x-4 border-b border-border pb-2 text-[11.5px] text-text-faint', cols)}>
                    <span role="columnheader">Step</span>
                    <span role="columnheader" className="relative h-4" aria-label="When it ran">
                        {marks.map((t, i) => (
                            <span
                                key={t}
                                className={cn('absolute whitespace-nowrap tabular-nums', i === 0 ? '' : '-translate-x-1/2', i === marks.length - 1 && marks.length > 1 && '-translate-x-full')}
                                style={{ left: pct(t) }}
                            >
                                {duration(t)}
                            </span>
                        ))}
                    </span>
                    {withCost && <span role="columnheader" className="text-right">Cost</span>}
                </div>
                {rows.map((row) => {
                    const entry = row.index !== undefined ? run.history[row.index] : undefined;
                    const { usage, credits } = row.index !== undefined ? entrySpend(run, row.index) : { usage: undefined, credits: undefined };
                    const expandable = Boolean(entry && hasDetail(entry, run.sharedState, usage));
                    const isOpen = open.has(row.key);
                    const width = row.duration === null ? Math.max(0, span - row.start) : row.duration;
                    return (
                        <div key={row.key} role="rowgroup">
                            <div role="row" className={cn('grid items-center gap-x-4 border-b border-border py-2.5', cols)}>
                                <div role="cell" className="min-w-0" style={{ paddingLeft: row.depth ? `${row.depth * 16}px` : undefined }}>
                                    <div className={cn('grid gap-0.5', row.depth > 0 && 'border-l border-border-strong pl-3')}>
                                        <RowName row={row} expandable={expandable} isOpen={isOpen} onToggle={() => toggle(row.key)} href={row.nested ? host.links.run(row.nested.run_id) : undefined} />
                                        <span className="truncate text-xs text-text-faint">
                                            <SubLine row={row} usageModels={usage?.models} />
                                        </span>
                                    </div>
                                </div>
                                <div role="cell" className="relative h-[18px] rounded" aria-label={row.duration === null ? `started at ${duration(row.start)}, still going` : `started at ${duration(row.start)}, took ${duration(row.duration)}`}>
                                    {gridLines}
                                    <span
                                        className={cn(
                                            'absolute top-1 h-2.5 min-w-[5px] rounded-[3px]',
                                            row.status === 'failed' ? 'bg-danger' : row.nested ? 'bg-border-strong' : BAR[row.role],
                                            row.duration === null && 'motion-safe:animate-pulse',
                                        )}
                                        style={{ left: pct(row.start), width: pct(width) }}
                                    />
                                </div>
                                {withCost && (
                                    <span role="cell" className="text-right text-[13px] tabular-nums text-text-muted">
                                        {row.nested ? money(row.nested.total_cost_usd ?? null) : usage && usage.cost_usd !== null ? money(usage.cost_usd) : '—'}
                                        {!row.nested && credits ? <span className="block text-[11px] text-text-faint">{creditsText(credits)}</span> : null}
                                    </span>
                                )}
                            </div>
                            {expandable && isOpen && entry && (
                                <div className="border-b border-border py-2.5" style={{ paddingLeft: row.depth ? `${row.depth * 16}px` : undefined }}>
                                    <StepDetail id={`step-output-${row.key}`} step={entry} sharedState={run.sharedState} usage={usage} removed={Boolean(run.stepDataRemovedAt)} />
                                </div>
                            )}
                        </div>
                    );
                })}
            </div>
            <div className="flex flex-wrap gap-x-[18px] gap-y-1.5 text-xs text-text-faint">
                {(['model', 'logic', 'person'] as StepRole[]).map((role) => (
                    <span key={role} className="inline-flex items-center gap-1.5">
                        <i aria-hidden className={cn('inline-block h-1.5 w-3.5 rounded-[3px]', BAR[role])} />
                        {/* Only once it is over: a running Parallel's model steps have no row yet. */}
                        {ROLE_LABEL[role]}{roles.has(role) || live ? '' : ' (none in this run)'}
                    </span>
                ))}
            </div>
        </div>
    );
}

function RowName({ row, expandable, isOpen, onToggle, href }: {
    row: TimelineRow; expandable: boolean; isOpen: boolean; onToggle: () => void; href?: string;
}) {
    const name = (
        <span className="truncate font-medium text-text">
            {row.name}
            {row.attempt ? <span className="font-normal text-text-faint"> · {ordinal(row.attempt)} run</span> : null}
        </span>
    );
    if (href) {
        return <Link href={href} className="flex min-w-0 items-center gap-2 hover:underline">{name}</Link>;
    }
    if (!expandable) return <span className="flex min-w-0 items-center gap-2 pl-6">{name}</span>;
    return (
        <button
            type="button"
            onClick={onToggle}
            aria-expanded={isOpen}
            aria-controls={`step-output-${row.key}`}
            aria-label={`${isOpen ? 'Hide' : 'Show'} output of ${row.name}`}
            className="flex min-w-0 items-center gap-2 rounded text-left"
        >
            <ChevronRight className={cn('size-4 shrink-0 text-text-faint transition-transform', isOpen && 'rotate-90')} aria-hidden />
            {name}
        </button>
    );
}

function SubLine({ row, usageModels }: { row: TimelineRow; usageModels?: string[] }) {
    if (row.nested) {
        const n = row.nested;
        return <>Nested run · <span className={cn(n.status === 'failed' && 'font-medium text-danger')}>{n.status}</span> · {n.steps_completed} step{n.steps_completed === 1 ? '' : 's'}</>;
    }
    const parts: React.ReactNode[] = [typeLabel(row.type)];
    if (usageModels?.length) parts.push(<span key="m" className="font-code" translate="no">{usageModels.join(', ')}</span>);
    if (row.status === 'failed') parts.push(<span key="f" className="font-medium text-danger">Failed</span>);
    else if (row.leftOpen) parts.push(<span key="o" className="font-medium text-text">{row.leftOpen}</span>);
    else if (row.status === 'paused') parts.push(<span key="w" className="font-medium text-person-ink">Needs input</span>);
    else if (row.status === 'cancelled') parts.push('Cancelled');
    parts.push(row.duration === null ? 'running' : duration(row.duration));
    return <>{parts.map((p, i) => <span key={i}>{i ? ' · ' : ''}{p}</span>)}</>;
}
