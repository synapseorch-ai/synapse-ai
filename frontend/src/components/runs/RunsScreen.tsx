'use client';
/*
 * Runs: every run of a workflow, newest first (D209, M1a).
 *
 * Until M1a the only table of runs sat on the Workflows landing, behind its
 * Active / Recent / All runs tabs, and "Runs & Logs" held the log viewer alone.
 * This page shows the same table with the same three views and the same
 * fetches; a run opens on the canvas through the address the landing already
 * keeps (`/workflows?run=<id>`). The log viewer is Logs, the tab beside this one.
 */
import { useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';

import { cn } from '@/lib/utils';
import { RunsTable, isLiveRun, type RunSummary, type RunsTableWorkflow } from './RunsTable';

type View = 'active' | 'recent' | 'all';

export function RunsScreen() {
    const router = useRouter();
    const [workflows, setWorkflows] = useState<RunsTableWorkflow[]>([]);
    const [schedules, setSchedules] = useState<{ id: string; name: string }[]>([]);
    const [activeRuns, setActiveRuns] = useState<RunSummary[]>([]);
    const [pastRuns, setPastRuns] = useState<RunSummary[]>([]);
    const [allRuns, setAllRuns] = useState<RunSummary[]>([]);
    // Until someone picks a view it follows the work, as the landing's tabs do:
    // Active while something is running or paused, All runs otherwise.
    const [chosen, setView] = useState<View | null>(null);

    useEffect(() => {
        fetch('/api/orchestrations').then(r => r.json()).then(data => {
            setWorkflows(Array.isArray(data) ? data : []);
        }).catch(() => {});
        // Schedule names, for labelling schedule-triggered runs.
        fetch('/api/schedules').then(r => r.json()).then(data => {
            setSchedules(Array.isArray(data) ? data : []);
        }).catch(() => {});
    }, []);

    // The same 5 s poll as the landing's: what is live, and what finished lately.
    useEffect(() => {
        const read = () => fetch('/api/orchestrations/runs')
            .then(r => r.json())
            .then(data => {
                if (Array.isArray(data)) {
                    setActiveRuns(data.filter(isLiveRun));
                    setPastRuns(data);
                }
            })
            .catch(() => {});
        read();
        const interval = setInterval(read, 5000);
        return () => clearInterval(interval);
    }, []);

    const view: View = chosen ?? (activeRuns.length > 0 ? 'active' : 'all');

    // The heavier list (up to 100 checkpoints) only while it is shown, every 10 s.
    useEffect(() => {
        if (view !== 'all') return;
        const read = () => fetch('/api/orchestrations/runs?limit=100')
            .then(r => r.json())
            .then(d => { if (Array.isArray(d)) setAllRuns(d); })
            .catch(() => {});
        read();
        const interval = setInterval(read, 10000);
        return () => clearInterval(interval);
    }, [view]);

    const views: { id: View; label: string; count: number | null }[] = [
        { id: 'active', label: 'Active', count: activeRuns.length },
        { id: 'recent', label: 'Recent', count: null },
        { id: 'all', label: 'All runs', count: null },
    ];
    const open = (run: RunSummary) => router.push(`/workflows?run=${encodeURIComponent(run.run_id)}`);

    return (
        <div className="space-y-4">
            <div role="group" aria-label="Show runs" className="flex w-fit items-center gap-1 rounded-md bg-surface-2 p-1">
                {views.map(v => (
                    <button
                        key={v.id}
                        type="button"
                        aria-pressed={view === v.id}
                        onClick={() => setView(v.id)}
                        className={cn(
                            'flex h-7 items-center gap-1.5 rounded px-3 text-xs font-medium transition-colors',
                            view === v.id ? 'bg-bg text-text shadow-sm' : 'text-text-muted hover:text-text',
                        )}
                    >
                        {v.label}
                        {v.count ? (
                            <span className="rounded-full bg-machine-subtle px-1.5 text-2xs text-accent-ink">{v.count}</span>
                        ) : null}
                    </button>
                ))}
            </div>
            {view === 'active' && (
                <RunsTable rows={activeRuns} empty="No active runs — start one from Workflows."
                    workflows={workflows} schedules={schedules} onOpen={open} />
            )}
            {view === 'recent' && (
                <RunsTable rows={pastRuns.filter(r => !isLiveRun(r)).slice(0, 10)} empty="No finished runs yet."
                    workflows={workflows} schedules={schedules} onOpen={open} />
            )}
            {view === 'all' && (
                <RunsTable rows={allRuns.length > 0 ? allRuns : pastRuns} empty="No runs recorded yet."
                    workflows={workflows} schedules={schedules} onOpen={open} />
            )}
        </div>
    );
}
