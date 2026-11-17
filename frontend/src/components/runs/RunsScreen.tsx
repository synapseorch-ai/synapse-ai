'use client';
/*
 * Runs: every run of a workflow, newest first, grouped by day (D209, M1a; the
 * list itself is M2b's shared `RunList`).
 *
 * The pills (All, Needs you, Running, Queued, Failed, Completed, Cancelled) take
 * the place of the Active / Recent / All runs views; Active is Running and
 * Needs you, and Today's group at the top is Recent. The same two reads as
 * before: the latest runs every 5 s, so a live run moves, and the latest 100
 * every 10 s, which the counts are of. A row opens the run's page,
 * `/runs/<id>`; the log viewer is Logs, the tab beside this one.
 */
import { useCallback, useEffect, useMemo, useState } from 'react';

import { RunList } from '@/components/run';
import { Button } from '@/components/ui';
import { ossRunRow } from '@/lib/runHost';
import type { Orchestration } from '@/types/orchestration';

import type { RunSummary } from './summary';

const ALL = 100;

export function RunsScreen() {
    const [workflows, setWorkflows] = useState<Orchestration[]>([]);
    const [schedules, setSchedules] = useState<{ id: string; name: string }[]>([]);
    const [recent, setRecent] = useState<RunSummary[]>([]);
    const [all, setAll] = useState<RunSummary[]>([]);
    const [loaded, setLoaded] = useState(false);
    // The runs before the latest hundred, a page at a time (F1639), and whether a page came back full.
    const [older, setOlder] = useState<RunSummary[]>([]);
    const [olderFull, setOlderFull] = useState(true);
    const [reading, setReading] = useState(false);

    useEffect(() => {
        fetch('/api/orchestrations').then(r => r.json()).then(data => {
            setWorkflows(Array.isArray(data) ? data : []);
        }).catch(() => {});
        // Schedule names, for labelling schedule-triggered runs.
        fetch('/api/schedules').then(r => r.json()).then(data => {
            setSchedules(Array.isArray(data) ? data : []);
        }).catch(() => {});
    }, []);

    // What is live, and what finished lately.
    useEffect(() => {
        const read = () => fetch('/api/orchestrations/runs')
            .then(r => r.json())
            .then(data => { if (Array.isArray(data)) setRecent(data); })
            .catch(() => {});
        read();
        const interval = setInterval(read, 5000);
        return () => clearInterval(interval);
    }, []);

    // The latest hundred, which the pills count.
    useEffect(() => {
        const read = () => fetch(`/api/orchestrations/runs?limit=${ALL}`)
            .then(r => r.json())
            .then(d => { if (Array.isArray(d)) setAll(d); })
            .catch(() => {})
            .finally(() => setLoaded(true));
        read();
        const interval = setInterval(read, 10000);
        return () => clearInterval(interval);
    }, []);

    // More past the latest hundred, from the last run read: its created_at and id (F1639).
    const hasOlder = all.length >= ALL && olderFull;
    const showOlder = useCallback(() => {
        const last = (older.length ? older : all).at(-1);
        if (!last?.created_at) return;
        setReading(true);
        const before = encodeURIComponent(`${last.created_at},${last.run_id}`);
        fetch(`/api/orchestrations/runs?limit=${ALL}&before=${before}`)
            .then(r => r.json())
            .then(d => {
                if (!Array.isArray(d)) return;
                setOlder((prev) => [...prev, ...d]);
                setOlderFull(d.length >= ALL);
            })
            .catch(() => {})
            .finally(() => setReading(false));
    }, [all, older]);

    // The hundred and what was read past it, with the fresher reads of the newest laid over them.
    const rows = useMemo(() => {
        const byId = new Map([...all, ...older].map((r) => [r.run_id, r]));
        for (const r of recent) byId.set(r.run_id, r);
        return [...byId.values()]
            .sort((a, b) => String(b.started_at ?? '').localeCompare(String(a.started_at ?? '')))
            .map((r) => ossRunRow(r, workflows, schedules));
    }, [all, older, recent, workflows, schedules]);

    return (
        <>
        <RunList
            rows={rows}
            workflows={workflows}
            hrefOf={(row) => `/runs/${encodeURIComponent(row.id)}`}
            caption={hasOlder ? `The latest ${all.length + older.length} runs.` : undefined}
            empty={
                <p className="px-2.5 py-10 text-center text-sm text-text-faint">
                    {loaded ? 'No runs recorded yet. Start one from Workflows.' : 'Reading runs…'}
                </p>
            }
        />
        {hasOlder && (
            <div className="flex justify-center py-4">
                <Button variant="secondary" size="sm" onClick={showOlder} loading={reading}>Show older</Button>
            </div>
        )}
        </>
    );
}
