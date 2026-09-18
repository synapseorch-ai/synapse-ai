'use client';
import { useCallback, useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';
import { Radio } from 'lucide-react';

type ActiveRun = {
    run_id: string;
    orchestration_id: string;
    status: string;
    started_at: string | null;
};

// Slim strip at the top of the main chat: orchestrations currently running or
// waiting for input, clickable through to their live run view
// (/orchestrations?run=<id>, which replays the event journal and
// tails it). Renders nothing when there is nothing active.
export function ActiveRunsBanner({ pollMs = 10000 }: { pollMs?: number }) {
    const router = useRouter();
    const [runs, setRuns] = useState<ActiveRun[]>([]);
    const [orchNames, setOrchNames] = useState<Record<string, string>>({});

    const refresh = useCallback(() => {
        fetch('/api/orchestrations/runs')
            .then(r => r.json())
            .then(data => {
                if (!Array.isArray(data)) return;
                setRuns(data.filter((r: ActiveRun) => r.status === 'running' || r.status === 'paused'));
            })
            .catch(() => {});
    }, []);

    useEffect(() => {
        refresh();
        const interval = setInterval(refresh, pollMs);
        // Refresh immediately when the tab becomes visible again.
        const onVisible = () => { if (document.visibilityState === 'visible') refresh(); };
        document.addEventListener('visibilitychange', onVisible);
        // Run notifications (needs-input / completed / failed) invalidate the
        // list instantly instead of waiting for the next poll tick.
        window.addEventListener('synapse-notification', refresh);
        return () => {
            clearInterval(interval);
            document.removeEventListener('visibilitychange', onVisible);
            window.removeEventListener('synapse-notification', refresh);
        };
    }, [refresh, pollMs]);

    // Resolve orchestration names lazily, whenever an unknown id appears.
    useEffect(() => {
        const unknown = runs.some(r => !(r.orchestration_id in orchNames));
        if (!unknown) return;
        fetch('/api/orchestrations')
            .then(r => r.json())
            .then(data => {
                if (!Array.isArray(data)) return;
                setOrchNames(prev => {
                    const next = { ...prev };
                    for (const o of data) next[o.id] = o.name;
                    return next;
                });
            })
            .catch(() => {});
    }, [runs, orchNames]);

    if (runs.length === 0) return null;

    return (
        <div className="px-4 py-1.5 border-b border-zinc-800 bg-zinc-900/70 backdrop-blur-sm">
            <div className="w-full md:max-w-5xl mx-auto flex items-center gap-2 flex-wrap">
                <span className="text-[11px] text-text-faint flex items-center gap-1 uppercase tracking-wider">
                    <Radio size={11} className="text-blue-400 animate-pulse" /> Orchestrations
                </span>
                {runs.map(run => (
                    <button
                        key={run.run_id}
                        onClick={() => router.push(`/orchestrations?run=${encodeURIComponent(run.run_id)}`)}
                        className={`flex items-center gap-1.5 px-2.5 py-0.5 rounded-md text-[11px] border transition-colors ${
                            run.status === 'paused'
                                ? 'bg-amber-950/40 border-amber-800/60 hover:bg-amber-900/40'
                                : 'bg-zinc-800 border-zinc-700 hover:bg-zinc-700'
                        }`}
                        title={run.run_id}
                    >
                        <span className={`w-1.5 h-1.5 rounded-full ${
                            run.status === 'running' ? 'bg-blue-400 animate-pulse' : 'bg-amber-400'
                        }`} />
                        <span className="text-zinc-300 truncate max-w-[180px]">
                            {orchNames[run.orchestration_id] ?? run.orchestration_id}
                        </span>
                        <span className={run.status === 'paused' ? 'text-amber-400' : 'text-text-faint'}>
                            {run.status === 'paused' ? 'needs input' : 'running'}
                        </span>
                    </button>
                ))}
            </div>
        </div>
    );
}
