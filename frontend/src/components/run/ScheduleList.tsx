'use client';

/*
 * The Schedules page (M2h): a quiet row per schedule, after Prototype 4 —
 *
 *   line 1 — the name, what it starts (a workflow or an agent), how often (a
 *            chip in words), when it runs next ("Paused" when switched off),
 *            how its last run went, the row's controls
 *   line 2 — the description, or the message it sends
 *
 * Both products draw it; each maps its schedules onto `ScheduleListRow`
 * (`scheduleList.ts`) and brings its own controls through `actions`, and opens
 * its own editor through `onOpen`. Colour only for meaning (D202): a finished
 * last run is a time, with no word. Nothing here reads an API.
 */
import Link from 'next/link';
import { Bot, CalendarClock, Workflow } from 'lucide-react';
import { useMemo, useState, type ReactNode } from 'react';

import { SearchInput } from '@/components/ui';
import { cn } from '@/lib/cn';

import { type Clock } from './model';
import { RUN_DOT } from './RunList';
import { useClock, When } from './When';
import { lastRunBucket, matchSchedule, type ScheduleListRow } from './scheduleList';

export function ScheduleList({
    rows, onOpen, actions, actionsWidth = '120px', runHref, empty, toolbar, className,
}: {
    rows: ScheduleListRow[];
    /** Open the schedule's editor. */
    onOpen: (row: ScheduleListRow) => void;
    /** The row's own buttons (Run now, Delete…). */
    actions?: (row: ScheduleListRow) => ReactNode;
    /** The actions column's width, so every row's controls line up. */
    actionsWidth?: string;
    /** A link to the last run, when the product has its page. */
    runHref?: (runId: string) => string;
    empty: ReactNode;
    toolbar?: ReactNode;
    className?: string;
}) {
    const clock = useClock();
    const [query, setQuery] = useState('');
    const shown = useMemo(() => rows.filter((r) => matchSchedule(r, query)), [rows, query]);

    const cols = ['minmax(0,1fr)', 'minmax(0,190px)', 'minmax(0,180px)', '150px', '170px', ...(actions ? [actionsWidth] : [])].join(' ');
    const phoneCols = ['minmax(0,1fr)', ...(actions ? ['auto'] : [])].join(' ');
    const gridStyle = { '--cols': cols, '--cols-phone': phoneCols } as React.CSSProperties;
    const grid = 'grid items-center gap-x-4 px-2.5 [grid-template-columns:var(--cols-phone)] lg:[grid-template-columns:var(--cols)]';

    if (rows.length === 0) return <div className={className}>{empty}</div>;

    return (
        <div className={cn('grid gap-4', className)}>
            <div className="flex min-h-[34px] flex-wrap items-center gap-2">
                <SearchInput value={query} onChange={setQuery} placeholder="Search schedules…" className="w-full max-w-none sm:w-72" />
                {toolbar}
            </div>
            <div>
                <div style={gridStyle} className={cn(grid, 'hidden h-8 border-b border-border text-xs font-medium text-text-faint lg:grid')}>
                    <span aria-hidden>Schedule</span>
                    <span aria-hidden>Starts</span>
                    <span aria-hidden>How often</span>
                    <span aria-hidden>Next run</span>
                    <span aria-hidden>Last run</span>
                    {actions && <span aria-hidden />}
                </div>
                {shown.length === 0 ? (
                    <p className="px-2.5 py-10 text-center text-sm text-text-faint">No schedules match “{query}”.</p>
                ) : (
                    <ul aria-label="Schedules" className="m-0 list-none border-t border-border p-0 lg:border-t-0">
                        {shown.map((row) => (
                            <Row
                                key={row.id} row={row} clock={clock} grid={grid} gridStyle={gridStyle}
                                onOpen={onOpen} actions={actions} runHref={runHref}
                            />
                        ))}
                    </ul>
                )}
            </div>
        </div>
    );
}

function TargetLabel({ row }: { row: ScheduleListRow }) {
    const Icon = row.target.kind === 'agent' ? Bot : Workflow;
    return (
        <span className="flex min-w-0 items-center gap-1.5 text-[13px] text-text-muted" title={row.target.name}>
            <Icon aria-hidden className="size-3.5 shrink-0 text-text-faint" />
            <span className="sr-only">{row.target.kind === 'agent' ? 'Agent' : 'Workflow'}: </span>
            <span className="truncate">{row.target.name}</span>
        </span>
    );
}

function WordsChip({ row }: { row: ScheduleListRow }) {
    return (
        <span
            data-schedule-words
            title={row.detail}
            className="inline-flex h-[22px] max-w-full items-center gap-1.5 rounded-md bg-surface-2 px-2 text-xs font-medium text-text-muted"
        >
            <CalendarClock aria-hidden className="size-3.5 shrink-0" />
            <span className="truncate">{row.words}</span>
        </span>
    );
}

function NextRun({ row, clock }: { row: ScheduleListRow; clock: Clock }) {
    if (!row.enabled) {
        return <span className="inline-flex h-[22px] items-center rounded-md border border-border px-2 text-xs font-medium text-text-muted">Paused</span>;
    }
    return row.nextAt ? <When iso={row.nextAt} clock={clock} /> : <span className="text-text-faint">—</span>;
}

function LastRun({ row, clock, runHref }: { row: ScheduleListRow; clock: Clock; runHref?: (id: string) => string }) {
    const bucket = lastRunBucket(row);
    if (!row.lastAt && !row.last) return <span className="text-text-faint">Never</span>;
    const at = <When iso={row.lastAt} clock={clock} />;
    const word =
        bucket === 'failed' ? <span className="font-medium text-danger">Failed</span>
            : bucket === 'needs' ? <span className="font-medium text-person-ink">Needs input</span>
                : bucket === 'running' ? <span className="text-accent-ink">Running</span>
                    : bucket === 'cancelled' ? <span>Cancelled</span>
                        : bucket === 'queued' ? <span>Queued</span> : null;
    const body = (
        <span className="inline-flex min-w-0 items-center gap-1.5">
            {bucket && <span aria-hidden className={cn('inline-block size-2 shrink-0 rounded-full', RUN_DOT[bucket].split(' ')[0])} />}
            {word}{word && <span aria-hidden>·</span>}{at}
        </span>
    );
    return row.last && runHref ? (
        <Link href={runHref(row.last.runId)} className="relative z-10 min-w-0 hover:text-text hover:underline" aria-label={`Open the last run of ${row.name}`}>
            {body}
        </Link>
    ) : body;
}

function Row({
    row, clock, grid, gridStyle, onOpen, actions, runHref,
}: {
    row: ScheduleListRow; clock: Clock; grid: string; gridStyle: React.CSSProperties;
    onOpen: (row: ScheduleListRow) => void; actions?: (row: ScheduleListRow) => ReactNode;
    runHref?: (id: string) => string;
}) {
    const stretch = 'after:absolute after:inset-0 after:content-[""]';
    const cell = 'hidden min-w-0 whitespace-nowrap text-[13px] tabular-nums text-text-muted lg:block';
    return (
        <li
            data-schedule-row={row.id}
            style={gridStyle}
            className={cn(
                grid,
                'relative min-h-[60px] gap-y-1 border-b border-border py-2.5 transition-colors hover:bg-row-hover',
                'has-[[data-row-open]:focus-visible]:outline has-[[data-row-open]:focus-visible]:outline-2',
                'has-[[data-row-open]:focus-visible]:-outline-offset-2 has-[[data-row-open]:focus-visible]:outline-accent',
            )}
        >
            <span className="min-w-0">
                <button
                    type="button"
                    data-row-open
                    data-no-focus-ring
                    onClick={() => onOpen(row)}
                    aria-label={`Open ${row.name}`}
                    title={row.name}
                    className={cn('block w-full truncate text-left text-sm font-medium text-text', stretch, !row.enabled && 'text-text-muted')}
                >
                    {row.name}
                </button>
            </span>
            <span className="hidden min-w-0 lg:block"><TargetLabel row={row} /></span>
            <span className="hidden min-w-0 lg:block"><WordsChip row={row} /></span>
            <span className={cell}><NextRun row={row} clock={clock} /></span>
            <span className={cn(cell, 'truncate')}><LastRun row={row} clock={clock} runHref={runHref} /></span>
            {actions && <span className="relative z-10 flex items-center justify-end gap-0.5">{actions(row)}</span>}

            {/* Line 2, under the name and across the row. */}
            <span style={{ gridColumn: '1 / -1' }} className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1.5 text-xs text-text-faint">
                {/* What line 1 has no room for below `lg`. */}
                <span className="flex w-full min-w-0 flex-wrap items-center gap-x-3 gap-y-1 lg:hidden">
                    <TargetLabel row={row} />
                    <WordsChip row={row} />
                    <span className="text-text-muted">Next <NextRun row={row} clock={clock} /></span>
                    <span className="text-text-muted">Last <LastRun row={row} clock={clock} runHref={runHref} /></span>
                </span>
                {row.sub && <span className="min-w-0 max-w-full truncate text-text-muted" title={row.sub}>{row.sub}</span>}
            </span>
        </li>
    );
}
