"use client";
/* eslint-disable @typescript-eslint/no-explicit-any */
/*
 * The overview: what is happening now, what it has cost, and what is in here.
 *
 * Built from the design system rather than hand-rolled, so it reads as the same
 * product as the cloud console: `Card`, `Stat`, `StatusBadge`, `EmptyState`.
 *
 * Deliberately not chart-heavy. Cost, requests, tokens and savings are single
 * headline numbers, so they are stat tiles rather than plots. The one plot is
 * spend by model, which is a magnitude comparison across a handful of named
 * things — a single-series horizontal bar list, so one hue, values
 * direct-labelled, and no legend to decode.
 */
import { useCallback, useEffect, useState } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import {
    Activity, ArrowRight, Bot, Clock, Coins, DollarSign, Hash, Server, Workflow,
} from 'lucide-react';

import { Card, CardBody, EmptyState, Stat, StatusBadge, TextLink } from '@/components/ui';
import { RESOURCE_SAVED_EVENT } from '@/components/app/BuilderProvider';
import { NOTIFICATION_EVENT } from '@/components/notifications/NotificationProvider';
import { usdCompact } from '@/lib/money';

type Run = {
    run_id: string;
    orchestration_id: string;
    status: string;
    started_at: string | null;
    waiting_for_human?: boolean;
};

type ModelStat = { model: string; estimated_cost: number };

type Summary = {
    total_cost: number;
    total_tokens: number;
    total_requests: number;
    total_estimated_savings?: number;
    by_model: ModelStat[];
};

const compact = (n: number) =>
    n >= 1e9 ? `${(n / 1e9).toFixed(1)}B`
        : n >= 1e6 ? `${(n / 1e6).toFixed(1)}M`
            : n >= 1e3 ? `${(n / 1e3).toFixed(1)}K`
                : String(n);

function since(iso: string | null): string {
    if (!iso) return '—';
    const secs = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
    if (secs < 60) return `${Math.floor(secs)}s`;
    if (secs < 3600) return `${Math.floor(secs / 60)}m`;
    if (secs < 86400) return `${Math.floor(secs / 3600)}h`;
    return `${Math.floor(secs / 86400)}d`;
}

export function Dashboard() {
    const router = useRouter();
    const [runs, setRuns] = useState<Run[]>([]);
    const [summary, setSummary] = useState<Summary | null>(null);
    const [counts, setCounts] = useState({ orchestrations: 0, agents: 0, schedules: 0, schedulesOn: 0, mcp: 0 });
    const [orchNames, setOrchNames] = useState<Record<string, string>>({});
    const [loaded, setLoaded] = useState(false);

    const refreshRuns = useCallback(() => {
        fetch('/api/orchestrations/runs')
            .then(r => r.json())
            .then(d => { if (Array.isArray(d)) setRuns(d); })
            .catch(() => { });
    }, []);

    useEffect(() => {
        refreshRuns();
        const interval = setInterval(refreshRuns, 10000);
        const onVisible = () => { if (document.visibilityState === 'visible') refreshRuns(); };
        document.addEventListener('visibilitychange', onVisible);
        window.addEventListener(NOTIFICATION_EVENT, refreshRuns);
        // The AI builder can add an agent or an orchestration from any screen,
        // and these are counts of exactly those.
        window.addEventListener(RESOURCE_SAVED_EVENT, refreshRuns);
        return () => {
            clearInterval(interval);
            document.removeEventListener('visibilitychange', onVisible);
            window.removeEventListener(NOTIFICATION_EVENT, refreshRuns);
            window.removeEventListener(RESOURCE_SAVED_EVENT, refreshRuns);
        };
    }, [refreshRuns]);

    useEffect(() => {
        const j = (url: string) => fetch(url).then(r => r.json()).catch(() => null);
        Promise.all([
            j('/api/usage/summary'), j('/api/orchestrations'), j('/api/agents'),
            j('/api/schedules'), j('/api/mcp/servers'),
        ]).then(([usage, orch, agents, schedules, mcp]) => {
            if (usage) setSummary(usage);
            const list = (v: any) => (Array.isArray(v) ? v : Array.isArray(v?.servers) ? v.servers : []);
            const sched = list(schedules);
            setOrchNames(Object.fromEntries(list(orch).map((o: any) => [o.id, o.name])));
            setCounts({
                orchestrations: list(orch).length,
                agents: list(agents).length,
                schedules: sched.length,
                schedulesOn: sched.filter((s: any) => s.enabled !== false).length,
                mcp: list(mcp).length,
            });
            setLoaded(true);
        });
    }, []);

    const live = runs.filter(r => r.status === 'running' || r.status === 'paused');
    const recent = [...runs]
        .sort((a, b) => (b.started_at ?? '').localeCompare(a.started_at ?? ''))
        .slice(0, 6);

    const models = (summary?.by_model ?? [])
        .slice()
        .sort((a, b) => b.estimated_cost - a.estimated_cost)
        .slice(0, 6);
    const maxCost = Math.max(...models.map(m => m.estimated_cost), 0);

    const runRow = (run: Run, tone: string) => (
        <button
            onClick={() => router.push(`/orchestrations?run=${encodeURIComponent(run.run_id)}`)}
            className="flex w-full items-center gap-3 rounded-md px-2 py-2 text-left transition-colors hover:bg-surface-2"
        >
            <span className="w-32 shrink-0">
                <StatusBadge status={run.status} waitingForHuman={run.waiting_for_human} />
            </span>
            <span className={`min-w-0 flex-1 truncate text-sm ${tone}`}>
                {orchNames[run.orchestration_id] ?? run.orchestration_id}
            </span>
            <span className="shrink-0 text-2xs tabular-nums text-text-faint">{since(run.started_at)}</span>
            <ArrowRight className="size-3.5 shrink-0 text-text-faint" aria-hidden />
        </button>
    );

    return (
        <div className="space-y-6">
            <Card
                title="Active now"
                actions={live.length > 0
                    ? <TextLink href="/orchestrations">All runs</TextLink>
                    : undefined}
            >
                {live.length === 0 ? (
                    <EmptyState title="Nothing running">
                        Start one from <TextLink href="/orchestrations">Orchestrations</TextLink>.
                    </EmptyState>
                ) : (
                    <CardBody>
                        <ul className="space-y-1">
                            {live.map(run => <li key={run.run_id}>{runRow(run, 'text-text')}</li>)}
                        </ul>
                    </CardBody>
                )}
            </Card>

            <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
                <Stat icon={DollarSign} label="Total spend" value={summary ? usdCompact(summary.total_cost) : '—'} hint="all recorded LLM calls" />
                <Stat icon={Activity} label="Requests" value={summary ? summary.total_requests.toLocaleString() : '—'} />
                <Stat icon={Hash} label="Tokens" value={summary ? compact(summary.total_tokens) : '—'} />
                <Stat icon={Coins} label="Cache saved" value={summary ? usdCompact(summary.total_estimated_savings ?? 0) : '—'} hint="vs uncached pricing" />
            </div>

            <div className="grid gap-6 lg:grid-cols-2">
                {/* One series, so one hue and no legend: the row label is the
                    identity and the value is direct-labelled beside it. */}
                <Card title="Spend by model" actions={<TextLink href="/usage">Usage</TextLink>}>
                    {models.length === 0 ? (
                        <EmptyState title="No usage recorded yet" />
                    ) : (
                        <CardBody>
                            <ul className="space-y-3">
                                {models.map(m => (
                                    <li key={m.model} title={`${m.model} — ${usdCompact(m.estimated_cost)}`}>
                                        <div className="mb-1.5 flex items-baseline justify-between gap-3">
                                            <span className="min-w-0 truncate text-sm text-text-muted">{m.model}</span>
                                            <span className="shrink-0 text-sm tabular-nums text-text">
                                                {usdCompact(m.estimated_cost)}
                                            </span>
                                        </div>
                                        <div className="h-2 w-full rounded-[4px] bg-surface-2">
                                            <div
                                                className="h-2 rounded-r-md-[4px] bg-accent"
                                                style={{ width: `${maxCost > 0 ? Math.max(2, (m.estimated_cost / maxCost) * 100) : 0}%` }}
                                            />
                                        </div>
                                    </li>
                                ))}
                            </ul>
                        </CardBody>
                    )}
                </Card>

                <Card title="Workspace">
                    <CardBody>
                        <ul className="divide-y divide-border">
                            {[
                                { icon: Workflow, label: 'Orchestrations', href: '/orchestrations', value: counts.orchestrations },
                                { icon: Bot, label: 'Agents', href: '/agents', value: counts.agents },
                                {
                                    icon: Clock, label: 'Schedules', href: '/schedules', value: counts.schedules,
                                    sub: counts.schedules > 0 ? `${counts.schedulesOn} enabled` : undefined,
                                },
                                { icon: Server, label: 'MCP servers', href: '/mcp-servers', value: counts.mcp },
                            ].map(row => (
                                <li key={row.href}>
                                    <Link href={row.href} className="flex items-center gap-3 py-2.5 transition-colors hover:text-text">
                                        <row.icon className="size-4 shrink-0 text-text-faint" aria-hidden />
                                        <span className="flex-1 text-sm text-text-muted">{row.label}</span>
                                        {row.sub && <span className="text-2xs text-text-faint">{row.sub}</span>}
                                        <span className="w-10 text-right text-sm tabular-nums text-text">
                                            {loaded ? row.value : '—'}
                                        </span>
                                    </Link>
                                </li>
                            ))}
                        </ul>
                    </CardBody>
                </Card>
            </div>

            <Card title="Recent runs" actions={<TextLink href="/runs">Runs &amp; Logs</TextLink>}>
                {recent.length === 0 ? (
                    <EmptyState title="No runs yet" />
                ) : (
                    <CardBody>
                        <ul className="divide-y divide-border">
                            {recent.map(run => <li key={run.run_id}>{runRow(run, 'text-text-muted')}</li>)}
                        </ul>
                    </CardBody>
                )}
            </Card>
        </div>
    );
}
