/* eslint-disable @typescript-eslint/no-explicit-any */
/**
 * The agent editor's read-only tabs: what this agent has done, what it cost,
 * and where it is wired in.
 *
 * All three read data that already existed and was simply not addressable per
 * agent: usage rows have carried `agent_id` since the usage log was a table,
 * agent run logs record it in their header, and orchestration steps and
 * schedules name the agent they call. So Runs and Metrics are one query
 * parameter each, and "Used by" needs no endpoint at all — it is a scan of the
 * two definition lists the workspace already loads.
 */
import { useCallback, useEffect, useState } from 'react';
import {
    Loader2, ScrollText, Coins, Workflow, Clock, MessageSquare, Bot, X,
} from 'lucide-react';

export type ActivityTab = 'runs' | 'metrics' | 'usedby';

interface AgentRun {
    run_id: string;
    started_at?: string;
    source?: string;
    session_id?: string;
    user_input?: string;
    file_size_kb?: number;
}

interface AgentMetrics {
    calls: number;
    input_tokens: number;
    output_tokens: number;
    total_tokens: number;
    cache_read_tokens: number;
    cache_write_tokens: number;
    estimated_cost: number;
    estimated_savings: number;
    last_used: string | null;
    first_used: string | null;
    by_model: { model: string; calls: number; total_tokens: number; estimated_cost: number }[];
}

const fmtInt = (n: number) => n.toLocaleString();
const fmtCost = (n: number) => (n >= 0.01 || n === 0 ? `$${n.toFixed(2)}` : `$${n.toFixed(4)}`);
const fmtDate = (s?: string | null) => {
    if (!s) return '—';
    const d = new Date(s);
    return isNaN(d.getTime()) ? s : d.toLocaleString();
};

const Empty = ({ icon: Icon, children }: { icon: any; children: React.ReactNode }) => (
    <div className="p-8 border border-dashed border-border text-center text-text-faint space-y-3">
        <Icon className="h-8 w-8 mx-auto opacity-20" />
        <p className="text-xs">{children}</p>
    </div>
);

const Spinner = () => (
    <div className="flex items-center gap-2 text-2xs text-text-faint p-4">
        <Loader2 className="h-3 w-3 animate-spin" /> Loading…
    </div>
);

// ─── Runs ─────────────────────────────────────────────────────────────────────

function RunsTab({ agentId }: { agentId: string }) {
    const [runs, setRuns] = useState<AgentRun[] | null>(null);
    const [openRun, setOpenRun] = useState<string | null>(null);
    const [content, setContent] = useState<string | null>(null);

    useEffect(() => {
        fetch(`/api/logs/agents?agent_id=${encodeURIComponent(agentId)}&limit=100`)
            .then(r => (r.ok ? r.json() : []))
            // Schedule-triggered runs are logged as agent runs but belong to the
            // schedule that caused them — the same split the Logs screen makes.
            .then(d => setRuns(Array.isArray(d) ? d.filter((l: AgentRun) => l.source !== 'schedule') : []))
            .catch(() => setRuns([]));
    }, [agentId]);

    const openLog = useCallback((runId: string) => {
        setOpenRun(runId);
        setContent(null);
        fetch(`/api/logs/agents/${runId}`)
            .then(r => (r.ok ? r.text() : 'Log not found.'))
            .then(setContent)
            .catch(() => setContent('Could not load this log.'));
    }, []);

    if (runs === null) return <Spinner />;
    if (runs.length === 0) {
        return <Empty icon={ScrollText}>This agent has not run yet. Runs appear here as soon as it does.</Empty>;
    }

    return (
        <div className="space-y-3">
            <p className="text-2xs text-text-faint">
                The last {runs.length} run{runs.length === 1 ? '' : 's'} of this agent. Scheduled
                runs are listed under Settings → Logs → Schedules.
            </p>
            <div className="border border-border rounded-md divide-y divide-border">
                {runs.map(run => (
                    <button
                        key={run.run_id}
                        onClick={() => openLog(run.run_id)}
                        className="w-full text-left p-3 hover:bg-surface-2/50 transition-colors block"
                    >
                        <div className="flex items-center justify-between gap-3">
                            <span className="text-xs text-text truncate flex-1 min-w-0">
                                {run.user_input || <span className="text-text-faint">(no prompt recorded)</span>}
                            </span>
                            <span className="text-2xs text-text-faint shrink-0">{fmtDate(run.started_at)}</span>
                        </div>
                        <div className="text-2xs text-text-faint mt-1 font-mono truncate">
                            {run.run_id}
                            {run.source ? ` · ${run.source}` : ''}
                            {run.session_id ? ` · session ${run.session_id.slice(0, 12)}` : ''}
                        </div>
                    </button>
                ))}
            </div>

            {openRun && (
                <div className="border border-border rounded-md">
                    <div className="flex items-center justify-between px-3 py-2 border-b border-border">
                        <span className="text-2xs font-mono text-text-muted truncate">{openRun}</span>
                        <button
                            onClick={() => { setOpenRun(null); setContent(null); }}
                            aria-label="Close log"
                            className="p-1 text-text-faint hover:text-text transition-colors"
                        >
                            <X className="h-3 w-3" />
                        </button>
                    </div>
                    {content === null ? <Spinner /> : (
                        <pre className="p-3 text-2xs font-mono text-text-muted whitespace-pre-wrap max-h-[400px] overflow-y-auto modern-scrollbar">
                            {content}
                        </pre>
                    )}
                </div>
            )}
        </div>
    );
}

// ─── Metrics ──────────────────────────────────────────────────────────────────

function MetricsTab({ agentId }: { agentId: string }) {
    const [m, setM] = useState<AgentMetrics | null>(null);
    const [failed, setFailed] = useState(false);

    useEffect(() => {
        fetch(`/api/usage/agents/${encodeURIComponent(agentId)}`)
            .then(r => (r.ok ? r.json() : Promise.reject(new Error('failed'))))
            .then(setM)
            .catch(() => setFailed(true));
    }, [agentId]);

    if (failed) return <Empty icon={Coins}>Usage could not be read for this agent.</Empty>;
    if (!m) return <Spinner />;
    if (m.calls === 0) {
        return <Empty icon={Coins}>No model calls recorded for this agent yet.</Empty>;
    }

    const tiles = [
        { label: 'Model calls', value: fmtInt(m.calls) },
        { label: 'Tokens in', value: fmtInt(m.input_tokens) },
        { label: 'Tokens out', value: fmtInt(m.output_tokens) },
        { label: 'Estimated cost', value: fmtCost(m.estimated_cost) },
        { label: 'Cache read', value: fmtInt(m.cache_read_tokens) },
        { label: 'Cache saved', value: fmtCost(m.estimated_savings) },
    ];

    return (
        <div className="space-y-4">
            <p className="text-2xs text-text-faint">
                Everything this agent has spent, from its first call {fmtDate(m.first_used)} to
                its last {fmtDate(m.last_used)}. Costs are estimates from the rate card under
                Settings → Usage.
            </p>
            <div className="grid grid-cols-2 md:grid-cols-3 gap-3">
                {tiles.map(t => (
                    <div key={t.label} className="p-3 border border-border bg-bg rounded-md">
                        <div className="text-2xs text-text-faint uppercase tracking-wider">{t.label}</div>
                        <div className="text-sm font-bold text-text mt-1 tabular-nums">{t.value}</div>
                    </div>
                ))}
            </div>

            <div>
                <h4 className="text-2xs font-bold text-text-faint uppercase tracking-wider mb-2">By model</h4>
                <div className="border border-border rounded-md divide-y divide-border">
                    {m.by_model.map(row => (
                        <div key={row.model} className="flex items-center justify-between gap-3 p-3">
                            <span className="text-xs text-text font-mono truncate flex-1 min-w-0">{row.model}</span>
                            <span className="text-2xs text-text-faint shrink-0 tabular-nums">
                                {fmtInt(row.calls)} calls · {fmtInt(row.total_tokens)} tokens · {fmtCost(row.estimated_cost)}
                            </span>
                        </div>
                    ))}
                </div>
            </div>
        </div>
    );
}

// ─── Used by ──────────────────────────────────────────────────────────────────

type UsedByRow = { key: string; primary: string; secondary?: string };

const Section = ({ icon: Icon, title, rows }: { icon: any; title: string; rows: UsedByRow[] }) => (
    rows.length === 0 ? null : (
        <div>
            <h4 className="text-2xs font-bold text-text-faint uppercase tracking-wider mb-2 flex items-center gap-1.5">
                <Icon className="h-3 w-3" /> {title}
            </h4>
            <div className="border border-border rounded-md divide-y divide-border">
                {rows.map(r => (
                    <div key={r.key} className="p-3">
                        <div className="text-xs text-text">{r.primary}</div>
                        {r.secondary && <div className="text-2xs text-text-faint mt-0.5">{r.secondary}</div>}
                    </div>
                ))}
            </div>
        </div>
    )
);

function UsedByTab({ agentId, channels, agents }: { agentId: string; channels: any[]; agents: any[] }) {
    const [orchestrations, setOrchestrations] = useState<any[] | null>(null);
    const [schedules, setSchedules] = useState<any[] | null>(null);

    useEffect(() => {
        Promise.all([
            fetch('/api/orchestrations').then(r => (r.ok ? r.json() : [])).catch(() => []),
            fetch('/api/schedules').then(r => (r.ok ? r.json() : [])).catch(() => []),
        ]).then(([o, s]) => {
            setOrchestrations(
                (Array.isArray(o) ? o : []).filter((orch: any) =>
                    (orch.steps || []).some((step: any) => step.agent_id === agentId),
                ),
            );
            setSchedules(
                (Array.isArray(s) ? s : []).filter(
                    (sch: any) => sch.target_type === 'agent' && sch.target_id === agentId,
                ),
            );
        });
    }, [agentId]);

    if (orchestrations === null || schedules === null) return <Spinner />;

    // An agent that delegates to this one dangles just as badly as a step does.
    const delegators = agents.filter(
        (a: any) => a.id !== agentId && (a.delegate_agent_ids || []).includes(agentId),
    );

    const nothing =
        orchestrations.length === 0 &&
        schedules.length === 0 &&
        channels.length === 0 &&
        delegators.length === 0;
    if (nothing) {
        return (
            <Empty icon={Workflow}>
                Nothing references this agent yet — no orchestration step, schedule, messaging
                channel or delegating agent.
            </Empty>
        );
    }

    return (
        <div className="space-y-4">
            <p className="text-2xs text-text-faint">
                Everywhere this agent is wired in. Deleting it will leave these pointing at nothing.
            </p>
            <Section
                icon={Workflow}
                title="Orchestrations"
                rows={orchestrations.map((o: any) => ({
                    key: o.id,
                    primary: o.name || o.id,
                    secondary: `${(o.steps || []).filter((s: any) => s.agent_id === agentId).length} step(s) call this agent`,
                }))}
            />
            <Section
                icon={Clock}
                title="Schedules"
                rows={schedules.map((s: any) => ({
                    key: s.id,
                    primary: s.name || s.id,
                    secondary: `${s.enabled ? 'enabled' : 'paused'}${s.next_run_at ? ` · next ${fmtDate(s.next_run_at)}` : ''}`,
                }))}
            />
            <Section
                icon={Bot}
                title="Delegating agents"
                rows={delegators.map((a: any) => ({
                    key: a.id,
                    primary: a.name || a.id,
                    secondary: 'may hand work to this agent',
                }))}
            />
            <Section
                icon={MessageSquare}
                title="Messaging channels"
                rows={channels.map((c: any) => ({
                    key: c.id,
                    primary: c.name,
                    secondary: `${c.platform}${c.agent_ids?.[0] === agentId ? ' · answers by default' : ''}`,
                }))}
            />
        </div>
    );
}

// ─── Entry point ──────────────────────────────────────────────────────────────

export const AgentActivity = ({
    agentId,
    tab,
    channels,
    agents,
}: {
    agentId: string;
    tab: ActivityTab;
    channels: any[];
    agents: any[];
}) => {
    if (!agentId) {
        return <Empty icon={ScrollText}>Save the agent first — there is nothing to show until it exists.</Empty>;
    }
    if (tab === 'runs') return <RunsTab agentId={agentId} />;
    if (tab === 'metrics') return <MetricsTab agentId={agentId} />;
    return <UsedByTab agentId={agentId} channels={channels} agents={agents} />;
};
