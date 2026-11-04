'use client';

/*
 * The runs list (M2b; UX1's Prototypes 2–4): every run newest first, grouped by
 * day, each row a quiet line — a status dot, the workflow and what started it,
 * its path, how long it took, its tokens and cost, when it started.
 *
 * Colour only for meaning (D202): a completed run is a dot and plain text; amber
 * is a run waiting on a person; red a failed one; cyan the machine at work.
 *
 * Both products draw their Runs page with it, and OSS's Workflows landing its
 * three run tabs. What a row opens is the caller's: a link to the run's page, or
 * an action in place (the landing restores the run onto its canvas).
 */
import Link from 'next/link';
import { useMemo, useState, type ReactNode } from 'react';

import { SearchInput, Select } from '@/components/ui';
import { cn } from '@/lib/cn';
import type { Orchestration } from '@/types/orchestration';

import { isCapStop, isCreditStop } from './failures';
import {
    DELETED_WORKFLOW,
    bucketOf, dayKey, dayLabel, glyphOf, money, ordinal, RUN_LIST_GLYPH_MAX, type Clock, type RunBucket, type RunRow,
} from './model';
import { PathGlyph } from './PathGlyph';
import { TimeOfDay, Took, useClock, When } from './When';

export type RunFilter = 'all' | RunBucket;

/** The pills, in order; every status the products filter by, plus Needs you. */
export const RUN_FILTERS: { id: RunFilter; label: string }[] = [
    { id: 'all', label: 'All' },
    { id: 'needs', label: 'Needs you' },
    { id: 'running', label: 'Running' },
    { id: 'queued', label: 'Queued' },
    { id: 'failed', label: 'Failed' },
    { id: 'completed', label: 'Completed' },
    { id: 'cancelled', label: 'Cancelled' },
];

export type RunSort =
    | 'newest' | 'oldest' | 'workflow' | 'status' | 'longest' | 'shortest'
    | 'tokens-most' | 'tokens-least' | 'cost-most' | 'cost-least';

const SORTS: { value: RunSort; label: string }[] = [
    { value: 'newest', label: 'Newest first' },
    { value: 'oldest', label: 'Oldest first' },
    { value: 'workflow', label: 'Workflow, A to Z' },
    { value: 'status', label: 'Status' },
    { value: 'longest', label: 'Longest first' },
    { value: 'shortest', label: 'Shortest first' },
    { value: 'tokens-most', label: 'Most tokens' },
    { value: 'tokens-least', label: 'Fewest tokens' },
    { value: 'cost-most', label: 'Most cost' },
    { value: 'cost-least', label: 'Least cost' },
];

/** A run's status dot, by bucket: quiet success, red failure, amber a person, cyan the machine. */
export const RUN_DOT: Record<RunBucket, string> = {
    completed: 'bg-success',
    failed: 'bg-danger',
    needs: 'bg-person motion-safe:animate-pulse',
    running: 'bg-machine motion-safe:animate-pulse',
    queued: 'bg-text-faint',
    cancelled: 'bg-text-faint',
};

const BUCKET_ORDER: RunBucket[] = ['needs', 'running', 'queued', 'failed', 'cancelled', 'completed'];

/** The status word under a run's name, for anything but a quiet success. */
export function StatusLine({ row }: { row: RunRow }) {
    const bucket = bucketOf(row.status, row.waitingForHuman);
    const step = row.currentStepName ? ` · ${row.currentStepName}` : '';
    switch (bucket) {
        case 'completed':
            return <>{row.trigger}</>;
        case 'running':
            return <><span className="text-accent-ink">Running</span>{step}</>;
        case 'needs':
            return <><span className="font-medium text-person-ink">Needs input</span>{step}</>;
        case 'queued':
            return <>Queued{row.queuePosition != null ? ` · ${ordinal(row.queuePosition)} in line` : ''}</>;
        case 'failed':
            // The credit gate stopped it (F444): the workspace ran out, not a step, so no step is named.
            if (isCreditStop(row.error)) return <span className="font-medium text-danger">Out of credits</span>;
            // Its own cap stopped it (CAP1): the run reached its limit, not a step.
            if (isCapStop(row.error)) return <span className="font-medium text-danger">Reached its cap</span>;
            return <><span className="font-medium text-danger">Failed</span>{step}</>;
        case 'cancelled':
            return <>Cancelled{step}</>;
    }
}

/** The judge's verdict (OSS): a flagged run reads red whatever it scored. */
function scoreMeta(row: RunRow): { cls: string; title: string } {
    if (row.score === null || row.score === undefined) return { cls: 'text-text-faint', title: 'Not scored' };
    if (row.scoreRegression) return { cls: 'font-medium text-danger', title: 'Scored below expectation' };
    if (row.score >= 7.5) return { cls: 'text-success', title: 'Scored well' };
    if (row.score >= 5) return { cls: 'text-text-muted', title: 'Scored moderately' };
    return { cls: 'text-danger', title: 'Scored poorly' };
}

const seconds = (r: RunRow, now: number) => {
    const a = r.startedAt ? Date.parse(r.startedAt) : NaN;
    if (Number.isNaN(a)) return -1;
    const b = r.endedAt ? Date.parse(r.endedAt) : now;
    return b - a;
};
const startOf = (r: RunRow) => r.startedAt || r.createdAt || '';

function sortRows(rows: RunRow[], sort: RunSort, now: number): RunRow[] {
    const out = [...rows];
    const by = <T,>(key: (r: RunRow) => T, dir: 1 | -1) =>
        out.sort((a, b) => {
            const x = key(a), y = key(b);
            return x < y ? -dir : x > y ? dir : 0;
        });
    switch (sort) {
        case 'newest': return by(startOf, -1);
        case 'oldest': return by(startOf, 1);
        case 'workflow': return by((r) => (r.workflowName ?? '').toLowerCase(), 1);
        case 'status': return by((r) => BUCKET_ORDER.indexOf(bucketOf(r.status, r.waitingForHuman)), 1);
        case 'longest': return by((r) => seconds(r, now), -1);
        case 'shortest': return by((r) => seconds(r, now), 1);
        case 'tokens-most': return by((r) => r.tokens ?? -1, -1);
        case 'tokens-least': return by((r) => r.tokens ?? Number.MAX_SAFE_INTEGER, 1);
        case 'cost-most': return by((r) => r.costUsd ?? -1, -1);
        case 'cost-least': return by((r) => r.costUsd ?? Number.MAX_SAFE_INTEGER, 1);
    }
}

export function RunList({
    rows, workflows, hrefOf, onOpen, filters = true, filter: controlled, onFilterChange,
    search = true, sortable = false, menu, toolbar, empty, caption, className,
}: {
    rows: RunRow[];
    /** The workflows, for each row's path glyph. */
    workflows: Orchestration[];
    /** A row's link (the run page). */
    hrefOf?: (row: RunRow) => string;
    /** Or an action in place, when there is no link. */
    onOpen?: (row: RunRow) => void;
    /** The pills. Off where the page has its own views (OSS's Workflows landing). */
    filters?: boolean;
    /** The picked pill, when the page keeps it (cloud keeps it in the address). */
    filter?: RunFilter;
    onFilterChange?: (filter: RunFilter) => void;
    search?: boolean;
    /** A Sort menu (cloud's table sorted by any column); the day groups are Newest first. */
    sortable?: boolean;
    /** A row's ⋯ menu. */
    menu?: (row: RunRow) => ReactNode;
    /** More controls beside the pills. */
    toolbar?: ReactNode;
    empty: ReactNode;
    /** What the list holds, said under it ("The latest 100 runs"). */
    caption?: string;
    className?: string;
}) {
    const clock = useClock();
    const [own, setOwn] = useState<RunFilter>('all');
    const filter = controlled ?? own;
    const setFilter = (f: RunFilter) => { setOwn(f); onFilterChange?.(f); };
    const [query, setQuery] = useState('');
    const [sort, setSort] = useState<RunSort>('newest');

    const byId = useMemo(() => new Map(workflows.map((w) => [w.id, w])), [workflows]);
    const counts = useMemo(() => {
        const c = Object.fromEntries(RUN_FILTERS.map((f) => [f.id, 0])) as Record<RunFilter, number>;
        for (const r of rows) { c.all += 1; c[bucketOf(r.status, r.waitingForHuman)] += 1; }
        return c;
    }, [rows]);

    const shown = useMemo(() => {
        const q = query.trim().toLowerCase();
        const kept = rows.filter((r) => {
            if (filters && filter !== 'all' && bucketOf(r.status, r.waitingForHuman) !== filter) return false;
            if (!q) return true;
            return [r.workflowName, r.id, r.trigger, r.currentStepName, r.status]
                .some((v) => v && String(v).toLowerCase().includes(q));
        });
        return sortRows(kept, sort, Number.isFinite(clock.now) ? clock.now : 0);
    }, [rows, query, filters, filter, sort, clock.now]);

    const hasTokens = rows.some((r) => r.tokens !== undefined);
    const hasScore = rows.some((r) => r.score !== null && r.score !== undefined);
    const grouped = sort === 'newest';
    const cols = [
        '14px', 'minmax(0,1.6fr)', '120px', '76px',
        ...(hasTokens ? ['80px'] : []), '76px', ...(hasScore ? ['56px'] : []), grouped ? '84px' : '150px',
        ...(menu ? ['36px'] : []),
    ].join(' ');
    // On a phone: the dot, the name, when it started, the menu.
    const phoneCols = ['14px', 'minmax(0,1fr)', 'auto', ...(menu ? ['36px'] : [])].join(' ');
    const gridStyle = { '--cols': cols, '--cols-phone': phoneCols } as React.CSSProperties;
    const grid = 'grid items-center gap-x-3.5 px-2.5 [grid-template-columns:var(--cols-phone)] sm:[grid-template-columns:var(--cols)]';
    const num = 'hidden text-right text-[13px] tabular-nums text-text-muted sm:block whitespace-nowrap';
    const numHead = 'hidden text-right sm:block';

    const groups: { key: string; label: string; rows: RunRow[] }[] = [];
    if (grouped) {
        for (const r of shown) {
            const iso = startOf(r);
            const key = dayKey(iso, clock);
            const last = groups[groups.length - 1];
            if (last && last.key === key) last.rows.push(r);
            else groups.push({ key, label: iso ? dayLabel(iso, clock) : 'Not started', rows: [r] });
        }
    } else {
        groups.push({ key: 'all', label: '', rows: shown });
    }

    const filterLabel = RUN_FILTERS.find((f) => f.id === filter)?.label.toLowerCase();

    return (
        <div className={cn('grid gap-4', className)}>
            {(filters || search || sortable || toolbar) && (
                <div className="flex flex-wrap items-center gap-2">
                    {filters && (
                        // One row that scrolls on a phone; wraps above that.
                        <div role="group" aria-label="Show runs" className="-mx-1 flex w-[calc(100%+0.5rem)] items-center gap-1.5 overflow-x-auto px-1 py-0.5 sm:mx-0 sm:w-auto sm:flex-wrap sm:overflow-visible sm:p-0">
                            {RUN_FILTERS.map((f) => (
                                <button
                                    key={f.id}
                                    type="button"
                                    aria-pressed={filter === f.id}
                                    onClick={() => setFilter(f.id)}
                                    className={cn(
                                        'inline-flex h-[30px] shrink-0 items-center gap-1.5 whitespace-nowrap rounded-full border px-3 text-[13px] font-medium transition-colors',
                                        filter === f.id
                                            ? 'border-transparent bg-text text-bg'
                                            : 'border-border text-text-muted hover:border-border-strong hover:text-text',
                                    )}
                                >
                                    {f.id !== 'all' && <span aria-hidden className={cn('size-2 rounded-full', RUN_DOT[f.id as RunBucket].split(' ')[0])} />}
                                    {f.label}
                                    <span className="text-[11.5px] tabular-nums opacity-70">{counts[f.id]}</span>
                                </button>
                            ))}
                        </div>
                    )}
                    {toolbar}
                    <span className="hidden flex-1 sm:block" />
                    {sortable && (
                        <div className="w-44">
                            <Select value={sort} onChange={setSort} options={SORTS} size="sm" aria-label="Sort runs" />
                        </div>
                    )}
                    {search && (
                        <SearchInput value={query} onChange={setQuery} placeholder="Search runs…" className="w-full max-w-none sm:w-60" />
                    )}
                </div>
            )}

            <div role="table" aria-label="Runs" aria-rowcount={shown.length + 1}>
                <div role="row" style={gridStyle} className={cn(grid, 'h-8 border-b border-border text-xs font-medium text-text-faint')}>
                    <span role="columnheader" aria-label="Status" />
                    <span role="columnheader">Workflow</span>
                    <span role="columnheader" className="hidden sm:block">Path</span>
                    <span role="columnheader" className={numHead}>Took</span>
                    {hasTokens && <span role="columnheader" className={numHead}>Tokens</span>}
                    <span role="columnheader" className={numHead}>Cost</span>
                    {hasScore && <span role="columnheader" className={numHead}>Score</span>}
                    <span role="columnheader" className="text-right">Started</span>
                    {menu && <span role="columnheader" aria-label="Actions" />}
                </div>

                {shown.length === 0 && (
                    rows.length === 0 ? empty : (
                        <p className="px-2.5 py-10 text-center text-sm text-text-faint">
                            {query ? `No runs match “${query}”.` : `No ${filterLabel} runs${caption ? ` in ${caption.replace(/^The /, 'the ')}` : ''}.`}
                        </p>
                    )
                )}

                {groups.map((g) => (
                    <div key={g.key} role="rowgroup">
                        {grouped && (
                            <div role="row" className="border-b border-border px-2.5 pb-1.5 pt-[18px] text-xs font-semibold text-text-faint">
                                <span role="cell" suppressHydrationWarning>{g.label}</span>
                            </div>
                        )}
                        {g.rows.map((r) => (
                            <Row
                                key={r.id} row={r} clock={clock} grouped={grouped} grid={grid} gridStyle={gridStyle} num={num}
                                bars={glyphOf(byId.get(r.workflowId), r)} hasTokens={hasTokens} hasScore={hasScore}
                                href={hrefOf?.(r)} onOpen={onOpen} menu={menu}
                            />
                        ))}
                    </div>
                ))}
            </div>
            {caption && rows.length > 0 && <p className="text-xs text-text-faint">{caption}</p>}
        </div>
    );
}

function Row({
    row, clock, grouped, grid, gridStyle, num, bars, hasTokens, hasScore, href, onOpen, menu,
}: {
    row: RunRow; clock: Clock; grouped: boolean; grid: string; gridStyle: React.CSSProperties; num: string;
    bars: ReturnType<typeof glyphOf>; hasTokens: boolean; hasScore: boolean;
    href?: string; onOpen?: (row: RunRow) => void; menu?: (row: RunRow) => ReactNode;
}) {
    const bucket = bucketOf(row.status, row.waitingForHuman);
    const name = row.workflowName ?? DELETED_WORKFLOW;
    // The name is the row's one control, stretched over the whole row; the menu
    // sits above it. A keyboard reaches it with Tab and opens it with Enter (F219).
    const stretch = 'after:absolute after:inset-0 after:content-[""]';
    const label = `${name}, ${bucket === 'completed' ? 'completed' : bucket === 'needs' ? 'needs input' : bucket}, run ${row.id}`;
    const score = scoreMeta(row);
    const started = startOf(row);
    return (
        <div
            role="row"
            style={gridStyle}
            className={cn(
                grid,
                'relative min-h-[50px] border-b border-border transition-colors hover:bg-row-hover',
                // The row draws the focus ring for its stretched control (which has none of its own).
                'has-[[data-row-open]:focus-visible]:outline has-[[data-row-open]:focus-visible]:outline-2',
                'has-[[data-row-open]:focus-visible]:-outline-offset-2 has-[[data-row-open]:focus-visible]:outline-accent',
            )}
        >
            <span role="cell" className="flex items-center">
                <span aria-hidden className={cn('inline-block size-2 rounded-full', RUN_DOT[bucket])} />
            </span>
            <span role="cell" className="min-w-0 py-2">
                {href ? (
                    <Link href={href} data-row-open data-no-focus-ring aria-label={label} title={row.id} className={cn('block truncate text-sm font-medium text-text', stretch)}>
                        {name}
                    </Link>
                ) : (
                    <button
                        type="button"
                        data-row-open
                        data-no-focus-ring
                        onClick={() => onOpen?.(row)}
                        aria-label={`View run ${row.id}`}
                        title={row.id}
                        className={cn('block w-full truncate text-left text-sm font-medium text-text', stretch)}
                    >
                        {name}
                    </button>
                )}
                <span className="block truncate text-xs text-text-faint">
                    <StatusLine row={row} />
                </span>
            </span>
            {/* Held to its column (F482): the bars that fit, and clipped should a count run wide. */}
            <span role="cell" className="hidden min-w-0 overflow-hidden sm:block">
                <PathGlyph bars={bars} done={row.stepsDone} max={RUN_LIST_GLYPH_MAX} />
            </span>
            <span role="cell" className={num}><Took start={row.startedAt} end={row.endedAt} /></span>
            {hasTokens && <span role="cell" className={num}>{row.tokens == null ? '—' : row.tokens.toLocaleString()}</span>}
            <span role="cell" className={num} title={row.costHint}>{money(row.costUsd)}</span>
            {hasScore && (
                <span role="cell" className={cn(num, score.cls)} title={score.title}>
                    {row.score === null || row.score === undefined ? '—' : row.score.toFixed(1)}
                </span>
            )}
            <span role="cell" className="whitespace-nowrap text-right text-[13px] tabular-nums text-text-muted">
                {grouped ? <TimeOfDay iso={started} clock={clock} /> : <When iso={started} clock={clock} />}
            </span>
            {menu && <span role="cell" className="relative z-10 flex justify-end">{menu(row)}</span>}
        </div>
    );
}
