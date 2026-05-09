/* eslint-disable @typescript-eslint/no-explicit-any */
'use client';
import { useState, useEffect, useCallback } from 'react';
import { RefreshCw, Trash2, Bot, Workflow, ScrollText, Clock } from 'lucide-react';

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

export const LogsTab = () => {
    const [logType, setLogType] = useState<LogType>('agents');
    const [logs, setLogs] = useState<LogSummary[]>([]);
    const [selectedId, setSelectedId] = useState<string | null>(null);
    const [content, setContent] = useState<string | null>(null);
    const [loading, setLoading] = useState(false);
    const [contentLoading, setContentLoading] = useState(false);
    const [offset, setOffset] = useState(0);
    const [hasMore, setHasMore] = useState(true);
    const [filterById, setFilterById] = useState<string>('all');
    const LIMIT = 100;

    const fetchLogs = useCallback(async (isLoadMore = false) => {
        setLoading(true);
        if (!isLoadMore) {
            setSelectedId(null);
            setContent(null);
            setOffset(0);
            setHasMore(true);
        }

        const currentOffset = isLoadMore ? offset : 0;

        try {
            const res = await fetch(`/api/logs/${logType}?limit=${LIMIT}&offset=${currentOffset}`);
            if (res.ok) {
                const data = await res.json();
                if (isLoadMore) {
                    setLogs(prev => [...prev, ...data]);
                } else {
                    setLogs(data);
                }
                if (data.length < LIMIT) {
                    setHasMore(false);
                }
                setOffset(prev => isLoadMore ? prev + data.length : data.length);
            }
        } finally {
            setLoading(false);
        }
    }, [logType, offset]);

    useEffect(() => { fetchLogs(); setFilterById('all'); }, [logType]);

    const fetchContent = async (runId: string) => {
        setSelectedId(runId);
        setContentLoading(true);
        setContent(null);
        try {
            const res = await fetch(`/api/logs/${logType}/${runId}`);
            if (res.ok) setContent(await res.text());
        } finally {
            setContentLoading(false);
        }
    };

    const deleteLog = async (runId: string, e: React.MouseEvent) => {
        e.stopPropagation();
        const res = await fetch(`/api/logs/${logType}/${runId}`, { method: 'DELETE' });
        if (res.ok) {
            if (selectedId === runId) { setSelectedId(null); setContent(null); }
            setLogs(prev => prev.filter(l => l.run_id !== runId));
        }
    };

    // Under Agents tab: hide logs that were triggered by a schedule
    // (they appear as agent_logs with source='schedule' but belong in Schedules tab)
    const visibleLogs = logType === 'agents'
        ? logs.filter(l => l.source !== 'schedule')
        : logs;

    // Unique ID options for secondary dropdown (based on name, not run_id)
    const uniqueIds: string[] = Array.from(new Set(
        visibleLogs.map(log =>
            logType === 'agents' ? (log.agent_name || log.run_id)
                : logType === 'orchestrations' ? (log.orchestration_name || log.run_id)
                    : (log.schedule_name || log.run_id)
        ).filter(Boolean) as string[]
    ));

    // Apply secondary filter
    const filteredLogs = filterById === 'all' ? visibleLogs : visibleLogs.filter(log => {
        const name = logType === 'agents' ? (log.agent_name || log.run_id)
            : logType === 'orchestrations' ? (log.orchestration_name || log.run_id)
                : (log.schedule_name || log.run_id);
        return name === filterById;
    });

    // Group filtered logs by session_id
    const groupedLogs: Record<string, LogSummary[]> = {};
    const sessionOrder: string[] = [];

    filteredLogs.forEach(log => {
        const sid = log.session_id || `nosession_${log.run_id}`;
        if (!groupedLogs[sid]) {
            groupedLogs[sid] = [];
            sessionOrder.push(sid);
        }
        groupedLogs[sid].push(log);
    });

    const renderLogContent = (text: string) => {
        const segments: Array<{ type: 'text' | 'compact'; content: string }> = [];
        let lastIndex = 0;
        const re = /={10,}[\s\S]*?CONTEXT COMPACTED[\s\S]*?={10,}/g;
        let match: RegExpExecArray | null;
        while ((match = re.exec(text)) !== null) {
            if (match.index > lastIndex) {
                segments.push({ type: 'text', content: text.slice(lastIndex, match.index) });
            }
            segments.push({ type: 'compact', content: match[0] });
            lastIndex = match.index + match[0].length;
        }
        if (lastIndex < text.length) {
            segments.push({ type: 'text', content: text.slice(lastIndex) });
        }
        if (segments.length === 0) {
            return <pre className="text-zinc-300 whitespace-pre-wrap break-words">{text}</pre>;
        }
        return segments.map((seg, i) =>
            seg.type === 'compact' ? (
                <div key={i} className="my-3 border border-cyan-800/60 bg-cyan-950/30 px-4 py-3">
                    <pre className="text-cyan-300 whitespace-pre-wrap break-words">{seg.content}</pre>
                </div>
            ) : (
                <pre key={i} className="text-zinc-300 whitespace-pre-wrap break-words">{seg.content}</pre>
            )
        );
    };

    return (
        <div className="flex flex-col h-full overflow-hidden font-mono">

            <div className="px-6 py-4 border-b border-zinc-800 shrink-0 pr-14">
                <h1 className="text-2xl font-bold text-zinc-100">Logs</h1>
                <p className="text-zinc-500 text-xs mt-0.5">View All logs of Agent and Orchestration</p>
            </div>

            {/* Sub-tabs + controls */}
            <div className="flex items-center gap-2 px-6 py-3 border-b border-white/10 shrink-0">
                <button
                    onClick={() => setLogType('agents')}
                    className={`flex items-center gap-2 px-4 py-2 text-sm font-medium transition-all ${logType === 'agents'
                        ? 'bg-white text-black'
                        : 'text-zinc-400 hover:text-white hover:bg-white/5'
                        }`}
                >
                    <Bot className="h-3.5 w-3.5" />
                    Agent Logs
                </button>
                <button
                    onClick={() => setLogType('orchestrations')}
                    className={`flex items-center gap-2 px-4 py-2 text-sm font-medium transition-all ${logType === 'orchestrations'
                        ? 'bg-white text-black'
                        : 'text-zinc-400 hover:text-white hover:bg-white/5'
                        }`}
                >
                    <Workflow className="h-3.5 w-3.5" />
                    Orchestration Logs
                </button>
                <button
                    onClick={() => setLogType('schedules')}
                    className={`flex items-center gap-2 px-4 py-2 text-sm font-medium transition-all ${logType === 'schedules'
                            ? 'bg-white text-black'
                            : 'text-zinc-400 hover:text-white hover:bg-white/5'
                        }`}
                >
                    <Clock className="h-3.5 w-3.5" />
                    Schedule Logs
                </button>
                <div className="ml-auto flex items-center gap-3">
                    <span className="text-xs text-zinc-600">{logs.length} log{logs.length !== 1 ? 's' : ''}</span>
                    <button
                        onClick={() => fetchLogs()}
                        className="p-2 text-zinc-500 hover:text-white hover:bg-white/5 transition-colors"
                        title="Refresh"
                    >
                        <RefreshCw className={`h-4 w-4 ${loading ? 'animate-spin' : ''}`} />
                    </button>
                </div>
            </div>

            {/* Secondary filter -- only shown when there are multiple unique names */}
            {uniqueIds.length > 1 && (
                <div className="flex items-center gap-3 px-6 py-2 border-b border-white/5 shrink-0 bg-zinc-900/40">
                    <span className="text-[11px] text-zinc-600 shrink-0">Filter</span>
                    <select
                        value={filterById}
                        onChange={e => setFilterById(e.target.value)}
                        className="bg-zinc-800/60 border border-white/8 text-zinc-300 text-[11px] px-2.5 py-1 focus:outline-none focus:border-white/20 transition-colors cursor-pointer appearance-none pr-6 max-w-[260px]"
                        style={{ backgroundImage: `url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='10' viewBox='0 0 24 24' fill='none' stroke='%2371717a' stroke-width='2'%3E%3Cpath d='M6 9l6 6 6-6'/%3E%3C/svg%3E")`, backgroundRepeat: 'no-repeat', backgroundPosition: 'right 6px center' }}
                    >
                        <option value="all">All {logType === 'orchestrations' ? 'orchestrations' : logType === 'agents' ? 'agents' : 'schedules'}</option>
                        {uniqueIds.map(id => (
                            <option key={id} value={id}>{id.length > 40 ? id.slice(0, 37) + '...' : id}</option>
                        ))}
                    </select>
                </div>
            )}

            {/* Two-pane layout */}
            <div className="flex flex-1 overflow-hidden">

                {/* Left: log list */}
                <div className="w-80 border-r border-white/10 overflow-y-auto shrink-0 flex flex-col">
                    {loading && logs.length === 0 ? (
                        <div className="flex items-center justify-center h-32 text-zinc-600 text-xs">Loading...</div>
                    ) : logs.length === 0 ? (
                        <div className="flex flex-col items-center justify-center h-40 gap-3 text-zinc-700">
                            <ScrollText className="h-8 w-8" />
                            <p className="text-xs">No logs yet</p>
                        </div>
                    ) : (
                        <>
                            {sessionOrder.map(sid => {
                                const sessionLogs = groupedLogs[sid];
                                const sessionInput = sessionLogs.find(l => l.user_input || l.prompt)?.user_input
                                    || sessionLogs.find(l => l.prompt)?.prompt;
                                const sessionDisplayName = sid.startsWith('nosession_') ? 'Individual Run' : `Session: ${sid.slice(-8)}`;

                                return (
                                    <div key={sid} className="border-b border-white/10 last:border-b-0">
                                        <div className="bg-white/5 px-4 py-2 border-b border-white/5 flex flex-col gap-0.5">
                                            <span className="text-[10px] font-bold text-zinc-500 uppercase tracking-wider">{sessionDisplayName}</span>
                                            {sessionInput && (
                                                <span className="text-[10px] text-zinc-400 truncate italic">"{sessionInput}"</span>
                                            )}
                                        </div>
                                        {sessionLogs.map(log => {
                                            const isSelected = selectedId === log.run_id;
                                            const name = logType === 'agents'
                                                ? (log.agent_name || log.run_id)
                                                : logType === 'orchestrations'
                                                    ? (log.orchestration_name || log.run_id)
                                                    : (log.schedule_name || log.run_id);
                                            const subtitle = logType === 'agents'
                                                ? log.source
                                                : logType === 'orchestrations'
                                                    ? log.orchestration_id
                                                    : log.target_type;

                                            return (
                                                <div
                                                    key={log.run_id}
                                                    onClick={() => fetchContent(log.run_id)}
                                                    className={`group cursor-pointer px-4 py-3 border-b border-white/5 last:border-b-0 flex flex-col gap-1 transition-colors ${isSelected
                                                        ? 'bg-white/10 border-l-2 border-l-white'
                                                        : 'hover:bg-white/5 border-l-2 border-l-transparent'
                                                        }`}
                                                >
                                                    <div className="flex items-start justify-between gap-2">
                                                        <span className="text-xs text-white font-semibold leading-tight truncate">{name}</span>
                                                        <button
                                                            onClick={(e) => deleteLog(log.run_id, e)}
                                                            className="opacity-0 group-hover:opacity-100 p-0.5 text-zinc-600 hover:text-red-400 transition-all shrink-0 mt-0.5"
                                                            title="Delete log"
                                                        >
                                                            <Trash2 className="h-3 w-3" />
                                                        </button>
                                                    </div>
                                                    {subtitle && (
                                                        <span className="text-[10px] text-zinc-500 truncate">{subtitle}</span>
                                                    )}
                                                    <div className="flex items-center justify-between">
                                                        {log.started_at && (
                                                            <span className="text-[10px] text-zinc-600">{log.started_at}</span>
                                                        )}
                                                        {log.file_size_kb !== undefined && (
                                                            <span className="text-[10px] text-zinc-700">{log.file_size_kb}KB</span>
                                                        )}
                                                    </div>
                                                </div>
                                            );
                                        })}
                                    </div>
                                );
                            })}

                            {hasMore && (
                                <button
                                    onClick={() => fetchLogs(true)}
                                    disabled={loading}
                                    className="w-full py-4 text-xs text-zinc-500 hover:text-white hover:bg-white/5 transition-colors disabled:opacity-50"
                                >
                                    {loading ? 'Loading more...' : 'Load More'}
                                </button>
                            )}
                        </>
                    )}
                </div>

                {/* Right: log content */}
                <div className="flex-1 overflow-hidden bg-zinc-950">
                    {contentLoading ? (
                        <div className="flex items-center justify-center h-full text-zinc-600 text-xs">
                            Loading log...
                        </div>
                    ) : content ? (
                        <div className="h-full overflow-auto p-6 font-mono text-[11px] leading-relaxed">
                            {renderLogContent(content)}
                        </div>
                    ) : (
                        <div className="flex flex-col items-center justify-center h-full gap-3 text-zinc-700">
                            <ScrollText className="h-10 w-10" />
                            <p className="text-xs">Select a log to view its contents</p>
                        </div>
                    )}
                </div>

            </div>
        </div>
    );
};
