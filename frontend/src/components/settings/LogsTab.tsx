'use client';

/*
 * Runs › Logs (M2h): every agent, workflow and schedule log, beside the one
 * that's open. The rows, the pills and the transcript are the shared
 * `components/run` pieces cloud draws too; this file reads OSS's listings and
 * runs and keeps OSS's own controls (the name filter, Load more, Delete).
 *
 * A workflow log's id is its run's, so the runs list (one read of the newest
 * 200) says how each of those runs went, how long it took and what it cost.
 * Agent logs carry no status in their listing and show none.
 */
import { Bot, Clock, RefreshCw, Trash2, Workflow } from 'lucide-react';
import Link from 'next/link';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { AppScreen } from '@/components/app/AppScreen';
import { parseStamp } from '@/components/LocalTime';
import { LogHead, LogKinds, LogList, Transcript, type LogListRow, type LogRun } from '@/components/run';
import type { RunSummary } from '@/components/runs/summary';
import { buttonStyles, Combobox, EmptyState, IconButton } from '@/components/ui';
import { cn } from '@/lib/cn';
import { ossLogRun } from '@/lib/runHost';

type LogType = 'agents' | 'orchestrations' | 'schedules';

interface LogSummary {
    run_id: string;
    // Agent log fields
    agent_name?: string;
    agent_id?: string;
    source?: string;
    session_id?: string;
    // Orchestration log fields
    orchestration_name?: string;
    orchestration_id?: string;
    // Schedule log fields
    schedule_name?: string;
    schedule_id?: string;
    target_type?: string;
    prompt?: string;
    // Common
    started_at?: string;
    user_input?: string;
    file_size_kb?: number;
}

const KINDS: { id: LogType; label: string; icon: React.ReactNode }[] = [
    { id: 'agents', label: 'Agent logs', icon: <Bot aria-hidden className="size-3.5" /> },
    { id: 'orchestrations', label: 'Workflow logs', icon: <Workflow aria-hidden className="size-3.5" /> },
    { id: 'schedules', label: 'Schedule logs', icon: <Clock aria-hidden className="size-3.5" /> },
];

const LIMIT = 100;
const RUNS_READ = 200;

const nameOf = (log: LogSummary, type: LogType) =>
    type === 'agents' ? (log.agent_name || log.run_id)
        : type === 'orchestrations' ? (log.orchestration_name || log.run_id)
            : (log.schedule_name || log.run_id);

const subOf = (log: LogSummary, type: LogType) =>
    type === 'agents' ? log.source : type === 'orchestrations' ? log.orchestration_id : log.target_type;

/** Written in UTC with no zone (F205): an ISO instant for `When`. */
const isoOf = (stamp?: string) => {
    if (!stamp) return null;
    const d = parseStamp(stamp);
    return Number.isNaN(d.getTime()) ? null : d.toISOString();
};

const NO_LOGS: LogSummary[] = [];

export const LogsTab = () => {
    const [logType, setLogType] = useState<LogType>('agents');
    const [logs, setLogs] = useState<LogSummary[]>([]);
    // The kind `logs` was read for (F503). While a switch's read is out, the old kind's logs
    // are not this kind's: the open one was fetched as the new kind's
    // (`/api/logs/orchestrations/agentrun_…`, a 404) until the new list arrived.
    const [logsType, setLogsType] = useState<LogType>('agents');
    const readNo = useRef(0);
    const [runs, setRuns] = useState<Map<string, LogRun>>(new Map());
    const [selectedId, setSelectedId] = useState<string | null>(null);
    // A click on the open log reads it again.
    const [reads, setReads] = useState(0);
    const [loading, setLoading] = useState(false);
    const [offset, setOffset] = useState(0);
    const [hasMore, setHasMore] = useState(true);
    const [filterById, setFilterById] = useState<string>('all');
    const detailRef = useRef<HTMLElement>(null);

    const fetchRuns = useCallback(() => {
        fetch(`/api/orchestrations/runs?limit=${RUNS_READ}`)
            .then(r => (r.ok ? r.json() : []))
            .then((d: RunSummary[]) => {
                if (Array.isArray(d)) setRuns(new Map(d.map(r => [r.run_id, ossLogRun(r)])));
            })
            .catch(() => {});
    }, []);

    const fetchLogs = useCallback(async (isLoadMore = false) => {
        // A read a newer one replaced (another kind, a refresh) is dropped when it lands.
        const mine = isLoadMore ? readNo.current : ++readNo.current;
        const type = logType;
        setLoading(true);
        if (!isLoadMore) {
            setSelectedId(null);
            setOffset(0);
            setHasMore(true);
            fetchRuns();
        }

        const currentOffset = isLoadMore ? offset : 0;
        let applied = false;

        try {
            const res = await fetch(`/api/logs/${type}?limit=${LIMIT}&offset=${currentOffset}`);
            if (res.ok && mine === readNo.current) {
                const data = await res.json();
                if (isLoadMore) {
                    setLogs(prev => [...prev, ...data]);
                } else {
                    setLogs(data);
                    setLogsType(type);
                }
                applied = true;
                if (data.length < LIMIT) {
                    setHasMore(false);
                }
                setOffset(prev => isLoadMore ? prev + data.length : data.length);
            }
        } finally {
            if (mine === readNo.current) {
                setLoading(false);
                // A failed read of another kind leaves that kind's list empty, not the old kind's.
                if (!isLoadMore && !applied && type !== logsType) {
                    setLogs(NO_LOGS);
                    setLogsType(type);
                }
            }
        }
    }, [logType, logsType, offset, fetchRuns]);

    // Only the open kind's logs: none while a switch's read is out.
    const current = logsType === logType ? logs : NO_LOGS;

    // eslint-disable-next-line react-hooks/exhaustive-deps
    useEffect(() => { fetchLogs(); setFilterById('all'); }, [logType]);

    const deleteLog = async (runId: string) => {
        const res = await fetch(`/api/logs/${logType}/${runId}`, { method: 'DELETE' });
        if (res.ok) {
            if (selectedId === runId) setSelectedId(null);
            setLogs(prev => prev.filter(l => l.run_id !== runId));
        }
    };

    // Under Agents: hide logs a schedule started (they belong under Schedule logs).
    const visibleLogs = useMemo(
        () => (logType === 'agents' ? current.filter(l => l.source !== 'schedule') : current),
        [current, logType],
    );

    // The names for the filter (by name, not run id).
    const uniqueIds: string[] = useMemo(
        () => Array.from(new Set(visibleLogs.map(log => nameOf(log, logType)).filter(Boolean))),
        [visibleLogs, logType],
    );

    const rows: LogListRow[] = useMemo(() => {
        const filteredLogs = filterById === 'all'
            ? visibleLogs
            : visibleLogs.filter(log => nameOf(log, logType) === filterById);

        // Grouped by session. A nested run's log goes under the run that started
        // it (F251), right after it: its id is that run's plus `__…`.
        const groupedLogs: Record<string, LogSummary[]> = {};
        const sessionOrder: string[] = [];
        const rootOf = (runId: string) => runId.split('__')[0];
        const groupOf = (log: LogSummary): string => {
            const root = rootOf(log.run_id);
            const rootLog = root !== log.run_id ? filteredLogs.find(l => l.run_id === root) : undefined;
            const anchor = rootLog ?? log;
            return anchor.session_id || `nosession_${root}`;
        };
        filteredLogs.forEach(log => {
            const sid = groupOf(log);
            if (!groupedLogs[sid]) {
                groupedLogs[sid] = [];
                sessionOrder.push(sid);
            }
            groupedLogs[sid].push(log);
        });
        const out: LogListRow[] = [];
        for (const sid of sessionOrder) {
            const group = groupedLogs[sid];
            const tops = group.filter(l => rootOf(l.run_id) === l.run_id);
            const placed = new Set<string>();
            const ordered: LogSummary[] = [];
            for (const top of tops) {
                ordered.push(top);
                placed.add(top.run_id);
                group.filter(l => l.run_id !== top.run_id && rootOf(l.run_id) === top.run_id)
                    .sort((a, b) => (a.started_at || '').localeCompare(b.started_at || ''))
                    .forEach(child => { ordered.push(child); placed.add(child.run_id); });
            }
            const all = [...ordered, ...group.filter(l => !placed.has(l.run_id))];
            const quote = all.find(l => l.user_input)?.user_input || all.find(l => l.prompt)?.prompt || null;
            // A session's logs sit under its heading; a run on its own needs none, and
            // what it was asked takes its second line.
            const alone = sid.startsWith('nosession_');
            const head = alone ? undefined : { key: sid, label: `Session: ${sid.slice(-8)}`, quote };
            for (const log of all) {
                const own = log.user_input || log.prompt;
                out.push({
                    id: log.run_id,
                    name: nameOf(log, logType),
                    sub: alone && own ? `“${own}”` : subOf(log, logType) ?? null,
                    nested: log.run_id.includes('__'),
                    sizeBytes: log.file_size_kb !== undefined ? log.file_size_kb * 1024 : null,
                    at: isoOf(log.started_at),
                    // A workflow's log is its run's; a schedule that started a workflow wrote the run's id too.
                    run: runs.get(log.run_id) ?? null,
                    group: head,
                });
            }
        }
        return out;
    }, [visibleLogs, filterById, logType, runs]);

    // The pane never opens empty: the newest log is open until another is picked.
    const openRow = rows.find(r => r.id === selectedId) ?? rows[0] ?? null;
    const openLog = openRow ? current.find(l => l.run_id === openRow.id) : undefined;
    const calledBy = logType === 'agents' && openLog?.source?.startsWith('orchestration:')
        ? openLog.source.slice('orchestration:'.length)
        : null;

    const open = (row: LogListRow) => {
        setSelectedId(row.id);
        setReads(n => n + 1);
        // Below the two columns the log sits under the list: bring it into view.
        if (typeof window !== 'undefined' && window.matchMedia('(max-width: 1023px)').matches) {
            requestAnimationFrame(() => detailRef.current?.scrollIntoView({ block: 'start', behavior: 'smooth' }));
        }
    };

    const load = useCallback(async (signal: AbortSignal) => {
        if (!openRow) return '';
        const res = await fetch(`/api/logs/${logType}/${encodeURIComponent(openRow.id)}`, { signal });
        if (!res.ok) {
            throw new Error(res.status === 404 ? 'This log is no longer on disk.' : `The log could not be read (${res.status}).`);
        }
        return res.text();
    }, [logType, openRow]);

    const actions = (
        <>
            <span className="text-xs tabular-nums text-text-faint">{current.length} log{current.length !== 1 ? 's' : ''}</span>
            <button
                type="button"
                aria-label="Refresh"
                title="Refresh"
                onClick={() => fetchLogs()}
                className={buttonStyles({ variant: 'ghost', size: 'md', iconOnly: true })}
            >
                <RefreshCw aria-hidden className={cn('size-4', loading && 'animate-spin')} />
            </button>
        </>
    );

    return (
        <AppScreen id="logs" width="wide" actions={actions}>
            <div className="grid gap-4">
                <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
                    <LogKinds kinds={KINDS} current={logType} onChange={setLogType} />
                    {uniqueIds.length > 1 && (
                        <div className="flex items-center gap-2">
                            <span className="text-xs text-text-faint">Filter</span>
                            <Combobox
                                value={filterById}
                                onChange={setFilterById}
                                aria-label="Filter by id"
                                searchPlaceholder="Search…"
                                size="sm"
                                className="w-[260px] max-w-full"
                                options={[
                                    { value: 'all', label: `All ${logType === 'orchestrations' ? 'workflows' : logType === 'agents' ? 'agents' : 'schedules'}` },
                                    ...uniqueIds.map(id => ({
                                        value: id,
                                        label: id.length > 40 ? `${id.slice(0, 37)}…` : id,
                                    })),
                                ]}
                            />
                        </div>
                    )}
                </div>

                {(loading || logsType !== logType) && current.length === 0 ? (
                    <p className="py-10 text-center text-sm text-text-faint">Loading...</p>
                ) : rows.length === 0 ? (
                    <EmptyState title="No logs yet">
                        A log is written every time an agent, a workflow or a schedule runs.
                    </EmptyState>
                ) : (
                    <div className="grid items-start gap-6 lg:grid-cols-[minmax(0,26rem)_minmax(0,1fr)]">
                        <LogList
                            rows={rows}
                            selected={openRow?.id ?? null}
                            onOpen={open}
                            empty={null}
                            actions={(row) => (
                                <IconButton
                                    label={`Delete log ${row.name}`}
                                    title="Delete log"
                                    icon={Trash2}
                                    size="sm"
                                    className="text-text-faint hover:bg-danger-subtle hover:text-danger"
                                    onClick={() => deleteLog(row.id)}
                                />
                            )}
                            footer={hasMore && (
                                <button
                                    type="button"
                                    onClick={() => fetchLogs(true)}
                                    disabled={loading}
                                    className="w-full py-3 text-[13px] font-medium text-text-muted transition-colors hover:bg-row-hover hover:text-text disabled:opacity-50"
                                >
                                    {loading ? 'Loading more...' : 'Load more'}
                                </button>
                            )}
                        />
                        <section
                            ref={detailRef}
                            aria-label="Log"
                            className="min-w-0 scroll-mt-4 space-y-4 lg:sticky lg:top-4"
                        >
                            {openRow && (
                                <>
                                    <LogHead row={openRow}>
                                        {calledBy && (
                                            <>
                                                <span aria-hidden>·</span>
                                                <span>Called by <Link href={`/runs/${encodeURIComponent(calledBy)}`} className="text-accent-ink underline-offset-2 hover:underline">this run</Link></span>
                                            </>
                                        )}
                                    </LogHead>
                                    <Transcript
                                        id={`${logType}/${openRow.id}#${reads}`}
                                        load={load}
                                        bodyClassName="max-h-[calc(100dvh-17rem)] min-h-[16rem]"
                                    />
                                </>
                            )}
                        </section>
                    </div>
                )}
            </div>
        </AppScreen>
    );
};
