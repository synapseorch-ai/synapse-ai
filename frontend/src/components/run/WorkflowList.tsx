'use client';

/*
 * The Workflows list (M2k): the hub's landing in both products. A quiet row per
 * workflow in the runs list's style (Naveen's call over Prototype 4's cards):
 *
 *   line 1 — the last run's dot, the name, what starts it (a chip, never an
 *            empty pill), the last run's status and time, how long it took and
 *            what it cost (`money`: `$0` free, `—` unpriced), the row's controls
 *   line 2 — the description, the steps by role (`StepPath`; a person's step is
 *            a filled amber tile, so no sentence repeats it — Naveen at review),
 *            how many, when it was last updated
 *
 * Colour only for meaning (D202). Each product maps its API onto
 * `WorkflowListRow` (`workflowRows.ts`) and brings its own controls through
 * `actions` and `menu`; cloud's selection and bulk delete come through `select`.
 * Nothing here reads an API.
 */
import Link from 'next/link';
import { CalendarClock, Play } from 'lucide-react';
import { useMemo, useState, type ReactNode } from 'react';

import { Checkbox, SearchInput, Select } from '@/components/ui';
import { cn } from '@/lib/cn';

import { money, type Clock } from './model';
import { RUN_DOT } from './RunList';
import { StepPath } from './StepPath';
import { Took, useClock, When } from './When';
import {
    lastBucket, matchWorkflow, sortWorkflows, WORKFLOW_SORTS,
    type WorkflowListRow, type WorkflowSort,
} from './workflowRows';

export function WorkflowList({
    rows, hrefOf, onOpen, actions, actionsWidth = '36px', menu, select, toolbar, empty,
    search = true, sortable = true, runsKnown = true, className,
}: {
    rows: WorkflowListRow[];
    /** Whether the rows' last runs have been read (OSS reads them after the workflows): until then, and when the
     *  read failed, a row with no run says "—", never "Never run". */
    runsKnown?: boolean;
    /** A row's link (cloud: the editor at `/workflows?id=`). */
    hrefOf?: (row: WorkflowListRow) => string;
    /** Or an action in place (OSS opens the workflow on its canvas). */
    onOpen?: (row: WorkflowListRow) => void;
    /** Buttons beside the menu (cloud's Run, Answer, Replay, Cancel). */
    actions?: (row: WorkflowListRow) => ReactNode;
    /** The actions column's width, so every row's controls line up. */
    actionsWidth?: string;
    /** A row's ⋯ menu. */
    menu?: (row: WorkflowListRow) => ReactNode;
    /** Rows picked for a bulk action (cloud's builders): the ids, and what to do with them. */
    select?: {
        selected: ReadonlySet<string>;
        onChange: (next: Set<string>) => void;
        /** The bulk bar's actions while something is picked (cloud's Delete). */
        bulk: ReactNode;
    };
    /** More controls on the toolbar's left. */
    toolbar?: ReactNode;
    empty: ReactNode;
    search?: boolean;
    sortable?: boolean;
    className?: string;
}) {
    const clock = useClock();
    const [query, setQuery] = useState('');
    const [sort, setSort] = useState<WorkflowSort>('updated-new');

    const shown = useMemo(
        () => sortWorkflows(rows.filter((r) => matchWorkflow(r, query)), sort),
        [rows, query, sort],
    );

    const hasControls = Boolean(actions || menu);
    const cols = [
        ...(select ? ['20px'] : []), '10px', 'minmax(0,1fr)', 'minmax(0,180px)', '200px', '64px', '72px',
        ...(hasControls ? [actionsWidth] : []),
    ].join(' ');
    // On a phone: the dot, the name, the controls; the rest moves under the name.
    const phoneCols = [...(select ? ['20px'] : []), '10px', 'minmax(0,1fr)', ...(hasControls ? ['auto'] : [])].join(' ');
    const gridStyle = { '--cols': cols, '--cols-phone': phoneCols } as React.CSSProperties;
    const grid = 'grid items-center gap-x-3.5 px-2.5 [grid-template-columns:var(--cols-phone)] sm:[grid-template-columns:var(--cols)]';

    const picked = select ? shown.filter((r) => select.selected.has(r.id)).length : 0;
    const allState = picked === 0 ? false : picked === shown.length ? true : 'indeterminate';
    const toggleAll = (on: boolean) => {
        if (!select) return;
        const next = new Set(select.selected);
        for (const r of shown) { if (on) next.add(r.id); else next.delete(r.id); }
        select.onChange(next);
    };

    if (rows.length === 0) return <div className={className}>{empty}</div>;

    return (
        <div className={cn('grid gap-4', className)}>
            <div className="flex min-h-[34px] flex-wrap items-center gap-2">
                {select && select.selected.size > 0 ? (
                    <div role="group" aria-label="Selected workflows" className="flex flex-wrap items-center gap-2">
                        <span className="text-sm font-medium text-text">{select.selected.size} selected</span>
                        {select.bulk}
                        <button
                            type="button"
                            onClick={() => select.onChange(new Set())}
                            className="h-8 rounded-md px-2.5 text-[13px] font-medium text-text-muted hover:bg-surface-2 hover:text-text"
                        >
                            Clear
                        </button>
                    </div>
                ) : (
                    <>
                        {search && (
                            <SearchInput
                                value={query}
                                onChange={setQuery}
                                placeholder="Search workflows…"
                                className="w-full max-w-none sm:w-72"
                            />
                        )}
                        {toolbar}
                    </>
                )}
                <span className="hidden flex-1 sm:block" />
                {sortable && (
                    <div className="w-full sm:w-56">
                        <Select value={sort} onChange={setSort} options={WORKFLOW_SORTS} size="sm" aria-label="Sort workflows" />
                    </div>
                )}
            </div>

            <div>
                <div style={gridStyle} className={cn(grid, 'h-8 border-b border-border text-xs font-medium text-text-faint')}>
                    {select && (
                        <span className="flex items-center">
                            {shown.length > 0 && (
                                <Checkbox
                                    checked={allState}
                                    onChange={toggleAll}
                                    label={allState === true ? 'Deselect all' : 'Select all'}
                                />
                            )}
                        </span>
                    )}
                    <span aria-hidden />
                    <span aria-hidden>Workflow</span>
                    <span aria-hidden className="hidden sm:block">Starts</span>
                    <span aria-hidden className="hidden sm:block">Last run</span>
                    <span aria-hidden className="hidden text-right sm:block">Took</span>
                    <span aria-hidden className="hidden text-right sm:block">Cost</span>
                    {hasControls && <span aria-hidden />}
                </div>

                {shown.length === 0 ? (
                    <p className="px-2.5 py-10 text-center text-sm text-text-faint">No workflows match “{query}”.</p>
                ) : (
                    <ul aria-label="Workflows" className="m-0 list-none p-0">
                        {shown.map((r) => (
                            <Row
                                key={r.id} row={r} clock={clock} runsKnown={runsKnown} grid={grid} gridStyle={gridStyle}
                                href={hrefOf?.(r)} onOpen={onOpen} actions={actions} menu={menu}
                                selected={select ? select.selected.has(r.id) : undefined}
                                onSelect={select ? (on) => {
                                    const next = new Set(select.selected);
                                    if (on) next.add(r.id); else next.delete(r.id);
                                    select.onChange(next);
                                } : undefined}
                            />
                        ))}
                    </ul>
                )}
            </div>
        </div>
    );
}

/** The last run, in words: quiet when it completed; amber, red or cyan otherwise. */
function LastRun({ row, clock, known }: { row: WorkflowListRow; clock: Clock; known: boolean }) {
    const run = row.lastRun;
    // Not read yet, or the read failed: no claim either way.
    if (!run && !known) return <span data-last-run-unknown className="text-text-faint">—</span>;
    if (!run) return <span className="text-text-faint">Never run</span>;
    const bucket = lastBucket(row);
    const at = <When iso={run.at} clock={clock} />;
    switch (bucket) {
        case 'needs': return <><span className="font-medium text-person-ink">Needs input</span> · {at}</>;
        case 'failed': return <><span className="font-medium text-danger">Failed</span> · {at}</>;
        case 'running': return <><span className="text-accent-ink">Running</span> · {at}</>;
        case 'queued': return <>Queued · {at}</>;
        case 'cancelled': return <>Cancelled · {at}</>;
        default: return at;
    }
}

function TriggerChip({ row }: { row: WorkflowListRow }) {
    const Icon = row.trigger.kind === 'schedule' ? CalendarClock : Play;
    return (
        <span
            data-trigger-chip={row.trigger.kind}
            title={row.trigger.detail ?? (row.trigger.kind === 'manual' ? 'Runs when someone starts it' : undefined)}
            className="inline-flex h-[22px] max-w-full items-center gap-1.5 rounded-md bg-surface-2 px-2 text-xs font-medium text-text-muted"
        >
            <Icon aria-hidden className="size-3.5 shrink-0" />
            <span className="truncate">{row.trigger.label}</span>
        </span>
    );
}

function Row({
    row, clock, runsKnown, grid, gridStyle, href, onOpen, actions, menu, selected, onSelect,
}: {
    row: WorkflowListRow; clock: Clock; runsKnown: boolean; grid: string; gridStyle: React.CSSProperties;
    href?: string; onOpen?: (row: WorkflowListRow) => void;
    actions?: (row: WorkflowListRow) => ReactNode; menu?: (row: WorkflowListRow) => ReactNode;
    selected?: boolean; onSelect?: (on: boolean) => void;
}) {
    const bucket = lastBucket(row);
    const run = row.lastRun;
    // The name is the row's one control, stretched over the whole row; the
    // checkbox and the row's buttons sit above it (RunList's rows, F219).
    const stretch = 'after:absolute after:inset-0 after:content-[""]';
    const open = cn('block truncate text-left text-sm font-medium text-text', stretch);
    const num = 'hidden whitespace-nowrap text-right text-[13px] tabular-nums text-text-muted sm:block';
    const steps = `${row.stepCount} ${row.stepCount === 1 ? 'step' : 'steps'}`;
    return (
        <li
            data-workflow-row={row.id}
            style={gridStyle}
            className={cn(
                grid,
                'relative min-h-[64px] gap-y-1 border-b border-border py-2.5 transition-colors hover:bg-row-hover',
                'has-[[data-row-open]:focus-visible]:outline has-[[data-row-open]:focus-visible]:outline-2',
                'has-[[data-row-open]:focus-visible]:-outline-offset-2 has-[[data-row-open]:focus-visible]:outline-accent',
            )}
        >
            {onSelect && (
                <span className="relative z-10 flex items-center">
                    <Checkbox checked={!!selected} onChange={onSelect} label={`Select row ${row.id}`} />
                </span>
            )}
            <span className="flex items-center" title={bucket ? undefined : 'Never run'}>
                <span
                    aria-hidden
                    className={cn(
                        'inline-block size-2 rounded-full',
                        bucket ? RUN_DOT[bucket] : 'border border-border-strong',
                    )}
                />
            </span>
            <span className="min-w-0">
                {href ? (
                    <Link href={href} data-row-open data-no-focus-ring aria-label={`Open ${row.name}`} title={row.name} className={open}>
                        {row.name}
                    </Link>
                ) : (
                    <button
                        type="button"
                        data-row-open
                        data-no-focus-ring
                        onClick={() => onOpen?.(row)}
                        aria-label={`Open ${row.name}`}
                        title={row.name}
                        className={cn(open, 'w-full')}
                    >
                        {row.name}
                    </button>
                )}
            </span>
            <span className="hidden min-w-0 sm:block"><TriggerChip row={row} /></span>
            <span className="hidden min-w-0 truncate whitespace-nowrap text-[13px] text-text-muted sm:block">
                <LastRun row={row} clock={clock} known={runsKnown} />
            </span>
            <span className={num}>{run?.startedAt ? <Took start={run.startedAt} end={run.endedAt} /> : '—'}</span>
            <span className={num} title={run?.costHint}>{run ? money(run.costUsd) : '—'}</span>
            {(actions || menu) && (
                <span className="relative z-10 flex items-center justify-end gap-0.5">
                    {actions?.(row)}
                    {menu?.(row)}
                </span>
            )}

            {/* Line 2, under the name and across the row. */}
            <span
                style={{ gridColumn: `${onSelect ? 3 : 2} / -1` }}
                className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1.5 text-xs text-text-faint sm:flex-nowrap"
            >
                {/* The phone's line 1 has no room for these. */}
                <span className="flex w-full min-w-0 flex-wrap items-center gap-x-3 gap-y-1 sm:hidden [&>*]:whitespace-nowrap">
                    <TriggerChip row={row} />
                    <span className="text-text-muted"><LastRun row={row} clock={clock} known={runsKnown} /></span>
                    {run?.startedAt && <span className="tabular-nums"><Took start={run.startedAt} end={run.endedAt} /></span>}
                    {run && <span className="tabular-nums">{money(run.costUsd)}</span>}
                </span>
                {row.description && (
                    <span className="min-w-0 max-w-full truncate text-text-muted sm:flex-1 sm:basis-0" title={row.description}>
                        {row.description}
                    </span>
                )}
                {row.types.length > 0 && (
                    <StepPath types={row.types} max={12} variant="inline" />
                )}
                <span className="shrink-0 whitespace-nowrap tabular-nums">{steps}</span>
                {row.updatedAt && (
                    <span className="hidden shrink-0 whitespace-nowrap xl:inline">
                        Updated <When iso={row.updatedAt} clock={clock} />
                    </span>
                )}
            </span>
        </li>
    );
}
