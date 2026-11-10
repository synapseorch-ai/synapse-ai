'use client';

/*
 * The Logs tab (M2h): its kinds as pills, the logs as quiet two-line rows in the
 * runs list's style, and the head over the open transcript. Both products draw
 * them; each maps its listing onto `LogListRow` (`logRows.ts`), matches its
 * workflow logs to the runs it has read, and brings its own controls (OSS's
 * Delete) through `actions`.
 *
 *   line 1 — the run's dot (neutral when no run was matched), the name, when
 *   line 2 — how the run went when it didn't simply finish, how long it took and
 *            what it cost (`money`), the size, then the source or what started it
 *
 * Colour only for meaning (D202): a finished run is a quiet dot with no word.
 * Nothing here reads an API.
 */
import { type ReactNode, useMemo } from 'react';

import { cn } from '@/lib/cn';

import { money, type Clock, type RunBucket } from './model';
import { ROW_CURRENT } from './DetailHead';
import { RUN_DOT } from './RunList';
import { Took, useClock, When } from './When';
import { groupLogs, logBucket, logSize, type LogListRow } from './logRows';

const PILL = 'inline-flex h-[30px] shrink-0 items-center gap-1.5 whitespace-nowrap rounded-full border px-3 text-[13px] font-medium transition-colors';

/** The log kinds, as the runs list's pills. */
export function LogKinds<K extends string>({
    kinds, current, onChange, label = 'Show logs',
}: {
    kinds: { id: K; label: string; title?: string; icon?: ReactNode }[];
    current: K;
    onChange: (kind: K) => void;
    label?: string;
}) {
    return (
        <div role="group" aria-label={label} className="flex flex-wrap items-center gap-1.5">
            {kinds.map((k) => (
                <button
                    key={k.id}
                    type="button"
                    aria-pressed={current === k.id}
                    title={k.title}
                    onClick={() => onChange(k.id)}
                    className={cn(
                        PILL,
                        current === k.id
                            ? 'border-transparent bg-text text-bg'
                            : 'border-border text-text-muted hover:border-border-strong hover:text-text',
                    )}
                >
                    {k.icon}
                    {k.label}
                </button>
            ))}
        </div>
    );
}

/** How a run went, in a word, when that is worth a word (a finished run says nothing). */
export function RunWord({ bucket }: { bucket: RunBucket | null }) {
    switch (bucket) {
        case 'needs': return <span className="font-medium text-person-ink">Needs input</span>;
        case 'failed': return <span className="font-medium text-danger">Failed</span>;
        case 'running': return <span className="text-accent-ink">Running</span>;
        case 'queued': return <span>Queued</span>;
        case 'cancelled': return <span>Cancelled</span>;
        default: return null;
    }
}

function Dot({ bucket }: { bucket: RunBucket | null }) {
    return (
        <span
            aria-hidden
            className={cn('inline-block size-2 rounded-full', bucket ? RUN_DOT[bucket] : 'border border-border-strong')}
        />
    );
}

export function LogList({
    rows, selected, onOpen, actions, label = 'Logs', empty, footer, className,
}: {
    rows: LogListRow[];
    selected: string | null;
    onOpen: (row: LogListRow) => void;
    /** The row's own buttons (OSS's Delete), shown on hover and focus. */
    actions?: (row: LogListRow) => ReactNode;
    label?: string;
    empty: ReactNode;
    /** Under the list (Load more, "Showing n of total"). */
    footer?: ReactNode;
    className?: string;
}) {
    const clock = useClock();
    const groups = useMemo(() => groupLogs(rows), [rows]);
    if (rows.length === 0) return <div className={className}>{empty}</div>;
    return (
        <div className={className}>
            <ul aria-label={label} className="m-0 list-none border-t border-border p-0">
                {groups.map(({ group, rows: inGroup }) => (
                    <li key={group?.key ?? inGroup[0].id} className="m-0 p-0">
                        {group && (
                            <div data-log-group className="min-w-0 px-2.5 pb-1.5 pt-4">
                                <p className="m-0 text-xs font-medium text-text-faint">{group.label}</p>
                                {group.quote && (
                                    <p className="m-0 truncate text-xs italic text-text-muted" title={group.quote}>
                                        “{group.quote}”
                                    </p>
                                )}
                            </div>
                        )}
                        <ul className="m-0 list-none p-0">
                            {inGroup.map((row) => (
                                <Row
                                    key={row.id} row={row} clock={clock} current={row.id === selected}
                                    onOpen={onOpen} actions={actions}
                                />
                            ))}
                        </ul>
                    </li>
                ))}
            </ul>
            {footer}
        </div>
    );
}

function Row({
    row, clock, current, onOpen, actions,
}: {
    row: LogListRow; clock: Clock; current: boolean;
    onOpen: (row: LogListRow) => void; actions?: (row: LogListRow) => ReactNode;
}) {
    const bucket = logBucket(row);
    const run = row.run;
    // The name is the row's one control, stretched over the row; its buttons sit above it.
    const stretch = 'after:absolute after:inset-0 after:content-[""]';
    const facts: ReactNode[] = [];
    const word = <RunWord bucket={bucket} />;
    if (row.nested) facts.push('Nested run');
    if (bucket && bucket !== 'completed') facts.push(word);
    if (run?.startedAt) facts.push(<span className="tabular-nums"><Took start={run.startedAt} end={run.endedAt} /></span>);
    if (run && run.costUsd !== undefined) facts.push(<span className="tabular-nums" title={run.costHint}>{money(run.costUsd)}</span>);
    if (row.sizeBytes != null) facts.push(<span className="tabular-nums">{logSize(row.sizeBytes)}</span>);
    return (
        <li
            data-log-row={row.id}
            className={cn(
                'group relative grid grid-cols-[10px_minmax(0,1fr)_auto] items-center gap-x-3 gap-y-0.5 border-b border-border px-2.5 py-2.5 transition-colors',
                current ? ROW_CURRENT : 'hover:bg-row-hover',
                row.nested && 'pl-6',
                'has-[[data-row-open]:focus-visible]:outline has-[[data-row-open]:focus-visible]:outline-2',
                'has-[[data-row-open]:focus-visible]:-outline-offset-2 has-[[data-row-open]:focus-visible]:outline-accent',
            )}
        >
            <span className="flex items-center" title={bucket ? undefined : 'No run to match'}><Dot bucket={bucket} /></span>
            <button
                type="button"
                data-row-open
                data-no-focus-ring
                aria-current={current ? 'true' : undefined}
                onClick={() => onOpen(row)}
                title={row.name}
                className={cn('block min-w-0 truncate text-left text-sm font-medium text-text', stretch)}
            >
                {row.name}
            </button>
            <span className="flex items-center justify-end gap-1">
                <When iso={row.at} clock={clock} className="whitespace-nowrap text-xs tabular-nums text-text-faint" />
                {actions && (
                    <span className="relative z-10 flex items-center sm:opacity-0 sm:transition-opacity sm:group-hover:opacity-100 sm:group-focus-within:opacity-100">
                        {actions(row)}
                    </span>
                )}
            </span>
            <span className="col-start-2 col-end-[-1] flex min-w-0 items-center gap-x-2 text-xs text-text-faint">
                {facts.map((f, i) => (
                    <span key={i} className="flex shrink-0 items-center gap-x-2">
                        {i > 0 && <span aria-hidden>·</span>}
                        {f}
                    </span>
                ))}
                {row.sub && (
                    <span className="flex min-w-0 items-center gap-x-2">
                        {facts.length > 0 && <span aria-hidden>·</span>}
                        <span className="truncate" title={row.sub}>{row.sub}</span>
                    </span>
                )}
            </span>
        </li>
    );
}

/** The head over the open transcript: the name, the id, how its run went, and the product's links. */
export function LogHead({ row, children }: { row: LogListRow; children?: ReactNode }) {
    const clock = useClock();
    const bucket = logBucket(row);
    const run = row.run;
    return (
        <div data-log-head className="min-w-0 space-y-1">
            <h2 className="m-0 truncate text-[15px] font-semibold text-text" title={row.name}>{row.name}</h2>
            <p className="m-0 truncate font-code text-xs text-text-faint">{row.id}</p>
            <p className="m-0 flex flex-wrap items-center gap-x-2 gap-y-1 text-[13px] text-text-muted">
                {bucket && (
                    <span className="flex items-center gap-1.5">
                        <Dot bucket={bucket} />
                        {bucket === 'completed' ? 'Completed' : <RunWord bucket={bucket} />}
                    </span>
                )}
                <When iso={run?.startedAt ?? row.at} clock={clock} />
                {run?.startedAt && <><span aria-hidden>·</span><span className="tabular-nums">Took <Took start={run.startedAt} end={run.endedAt} /></span></>}
                {run && run.costUsd !== undefined && <><span aria-hidden>·</span><span className="tabular-nums" title={run.costHint}>{money(run.costUsd)}</span></>}
                {children}
            </p>
        </div>
    );
}
