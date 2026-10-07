'use client';
/**
 * The open canvas's run drawer (M3a): the Run Log and Recent Runs that were
 * tabs under the old canvas, above the run strip. The log follows its newest
 * line while the reader is at its foot (F127), stops when they scroll up, and
 * "Jump to latest" starts it again.
 */
import { useEffect, useRef, useState } from 'react';
import { ArrowDown, X } from 'lucide-react';
import { IconButton, Spinner } from '@/components/ui';
import type { RunSummary } from '@/components/runs/summary';
import { NestedRuns, RunLogEntries, type LogEntry } from './runLog';
import type { RunDrawerView } from './RunStrip';

export function RunDrawer({
    view, onClose, runStatus, liveActivity, runLog, onOpenResponseModal, runId, humanPrompt, orchestrationNames,
    pastRuns, onRestoreRun,
}: {
    view: RunDrawerView;
    onClose: () => void;
    runStatus: string;
    liveActivity: string | null;
    runLog: LogEntry[];
    onOpenResponseModal: (entry: { step_name: string; step_type?: string; content: string }) => void;
    runId: string | null;
    humanPrompt: string | null;
    /** Orchestration names by id, to name a nested run (F251). */
    orchestrationNames: Record<string, string>;
    pastRuns: RunSummary[];
    onRestoreRun: (run: RunSummary) => void;
}) {
    const scrollRef = useRef<HTMLDivElement>(null);
    const [pinned, setPinned] = useState(true);
    if (runLog.length === 0 && !pinned) setPinned(true); // a new run starts at its foot

    useEffect(() => {
        const el = scrollRef.current;
        if (el && pinned && view === 'run') el.scrollTop = el.scrollHeight;
    }, [runLog, pinned, view]);

    const onScroll = () => {
        const el = scrollRef.current;
        if (el && view === 'run') setPinned(el.scrollHeight - el.scrollTop - el.clientHeight < 24);
    };

    return (
        <>
            <div className="flex shrink-0 items-center gap-2 border-b border-border px-3 pb-1.5">
                <h2 className="flex-1 text-sm font-semibold text-text">{view === 'run' ? 'Run log' : 'Recent runs'}</h2>
                <IconButton label={view === 'run' ? 'Close the run log' : 'Close recent runs'} icon={X} size="sm" onClick={onClose} />
            </div>
            <div ref={scrollRef} onScroll={onScroll} className="min-h-0 flex-1 overflow-y-auto p-3">
                {view === 'recent' ? (
                    <div className="space-y-1">
                        {pastRuns.length === 0 ? (
                            <div className="text-text-faint italic text-xs">No runs yet.</div>
                        ) : (
                            pastRuns.slice(0, 20).map(r => (
                                <div key={r.run_id} className="flex items-center gap-2 rounded-md bg-surface-2 px-2 py-1 text-[11px]">
                                    <span className={`size-1.5 shrink-0 rounded-full ${
                                        r.status === 'completed' ? 'bg-success' :
                                        r.status === 'failed'    ? 'bg-danger' :
                                        r.status === 'cancelled' ? 'bg-text-faint' :
                                        r.status === 'paused'    ? 'bg-person' : 'bg-machine motion-safe:animate-pulse'
                                    }`} />
                                    <span className="min-w-0 flex-1 truncate text-text-muted" title={r.run_id}>{r.run_id}</span>
                                    <span className="capitalize text-text-faint">{r.status}</span>
                                    {r.started_at && (
                                        <span className="text-[10px] text-text-faint">
                                            {new Date(r.started_at).toLocaleTimeString()}
                                        </span>
                                    )}
                                    {(r.status === 'failed' || r.status === 'cancelled') && (
                                        <button
                                            onClick={() => onRestoreRun(r)}
                                            className="rounded-md border border-border-strong bg-surface px-2 py-0.5 text-[10px] text-text hover:bg-surface-2"
                                        >
                                            Resume
                                        </button>
                                    )}
                                </div>
                            ))
                        )}
                    </div>
                ) : (
                    <div className="space-y-2">
                        {/* Live activity: what the model is doing right now */}
                        {runStatus === 'running' && liveActivity && (
                            <div className="flex items-center gap-2 rounded-md border border-border bg-machine-subtle px-2.5 py-1.5 text-[11px] text-text-muted">
                                <Spinner size={16} className="shrink-0 text-machine" />
                                <span className="truncate">{liveActivity}</span>
                            </div>
                        )}

                        <RunLogEntries runLog={runLog} onOpenResponseModal={onOpenResponseModal} />

                        {runId && (
                            <NestedRuns
                                runId={runId}
                                refreshKey={`${runStatus}:${runLog.length > 0}`}
                                names={orchestrationNames}
                            />
                        )}

                        {/* Not while a question is up: the run is waiting, nothing new
                            arrives, and the strip below says where it is. */}
                        {!pinned && !humanPrompt && runLog.length > 0 && (
                            <div className="pointer-events-none sticky bottom-0 z-10 flex justify-center pt-2">
                                <button
                                    type="button"
                                    onClick={() => setPinned(true)}
                                    className="pointer-events-auto inline-flex items-center gap-1 rounded-full border border-border bg-surface px-2.5 py-1 text-[11px] text-text-muted shadow-md transition-colors hover:text-text"
                                >
                                    <ArrowDown size={11} aria-hidden /> Jump to latest
                                </button>
                            </div>
                        )}
                    </div>
                )}
            </div>
        </>
    );
}
