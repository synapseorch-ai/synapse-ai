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
import Link from 'next/link';
import { Spinner, StatStrip } from '@/components/ui';
import { usd } from '@/lib/money';
import { parseStamp } from '@/components/LocalTime';
import {
    ScrollText, Coins, Workflow, Clock, MessageSquare, Bot, X, PanelTop, Phone,
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
// Run-log stamps are UTC with no zone, which `new Date` read as local time, so a
// run's start was off by the reader's offset (F205). parseStamp reads them as UTC.
const fmtDate = (s?: string | null) => {
    if (!s) return '—';
    const d = parseStamp(s);
    return isNaN(d.getTime()) ? s : d.toLocaleString();
};

const Empty = ({ icon: Icon, children }: { icon: any; children: React.ReactNode }) => (
    <div className="p-8 border border-dashed border-border text-center text-text-faint space-y-3">
        <Icon className="h-8 w-8 mx-auto opacity-20" />
        <p className="text-xs">{children}</p>
    </div>
);

/** The panel's own "loading a section" line, on the kit's spinner. */
const Loading = () => (
    <div className="flex items-center gap-2 p-4 text-2xs text-text-faint">
        <Spinner size={14} /> Loading…
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

    if (runs === null) return <Loading />;
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
                    {content === null ? <Loading /> : (
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
    if (!m) return <Loading />;
    if (m.calls === 0) {
        return <Empty icon={Coins}>No model calls recorded for this agent yet.</Empty>;
    }

    const tiles = [
        { label: 'Model calls', value: fmtInt(m.calls) },
        { label: 'Tokens in', value: fmtInt(m.input_tokens) },
        { label: 'Tokens out', value: fmtInt(m.output_tokens) },
        { label: 'Estimated cost', value: usd(m.estimated_cost) },
        { label: 'Cache read', value: fmtInt(m.cache_read_tokens) },
        { label: 'Cache saved', value: usd(m.estimated_savings) },
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
                                {fmtInt(row.calls)} calls · {fmtInt(row.total_tokens)} tokens · {usd(row.estimated_cost)}
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

/**
 * The workflows and schedules that call this agent: a scan of the two lists the
 * workspace already serves. Used by the "Used by" tab and by the setup's
 * "Where it works" (M2d), so both say the same thing.
 */
export function useAgentReferences(agentId: string | null) {
    const [refs, setRefs] = useState<{ id: string; orchestrations: any[]; schedules: any[] } | null>(null);
    useEffect(() => {
        if (!agentId) return;
        let live = true;
        Promise.all([
            fetch('/api/orchestrations').then(r => (r.ok ? r.json() : [])).catch(() => []),
            fetch('/api/schedules').then(r => (r.ok ? r.json() : [])).catch(() => []),
        ]).then(([o, s]) => {
            if (!live) return;
            setRefs({
                id: agentId,
                orchestrations: (Array.isArray(o) ? o : []).filter((orch: any) =>
                    (orch.steps || []).some((step: any) => step.agent_id === agentId),
                ),
                schedules: (Array.isArray(s) ? s : []).filter(
                    (sch: any) => sch.target_type === 'agent' && sch.target_id === agentId,
                ),
            });
        });
        return () => { live = false; };
    }, [agentId]);
    // Another agent's answer is not this one's: until this agent's arrives, it is loading.
    return refs && refs.id === agentId ? refs : null;
}

function UsedByTab({ agentId, channels, agents }: { agentId: string; channels: any[]; agents: any[] }) {
    const refs = useAgentReferences(agentId);
    if (refs === null) return <Loading />;
    const { orchestrations, schedules } = refs;

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
                Nothing references this agent yet — no workflow step, schedule, messaging
                channel or Team agent.
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
                title="Workflows"
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
                title="Team agents it is a member of"
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

// ─── The setup's summaries (M2d) ─────────────────────────────────────────────

/** The first moment of this month, in the reader's time. */
function monthStart(): number {
    const now = new Date();
    return new Date(now.getFullYear(), now.getMonth(), 1).getTime();
}

/** The most calls "This month" reads; past it the cost is a floor, and says so. */
const MONTH_CALLS = 1000;

/**
 * This month's runs and cost for one agent, from two reads that already exist:
 * the agent's run logs and its usage records (newest first). No endpoint sums a
 * month, so the page does; a month with more calls than one page reads says
 * "at least".
 */
export function ThisMonth({ agentId }: { agentId: string }) {
    const [month, setMonth] = useState<{ id: string; runs: number; cost: number; capped: boolean } | null>(null);
    const [failed, setFailed] = useState<string | null>(null);
    useEffect(() => {
        let live = true;
        const since = monthStart();
        Promise.all([
            fetch(`/api/logs/agents?agent_id=${encodeURIComponent(agentId)}&limit=500`).then(r => (r.ok ? r.json() : Promise.reject(new Error('runs')))),
            fetch(`/api/usage/logs?agent_id=${encodeURIComponent(agentId)}&limit=${MONTH_CALLS}`).then(r => (r.ok ? r.json() : Promise.reject(new Error('usage')))),
        ]).then(([runs, usage]) => {
            if (!live) return;
            const inMonth = (stamp?: string | null) => {
                if (!stamp) return false;
                const t = parseStamp(stamp).getTime();
                return !isNaN(t) && t >= since;
            };
            const runCount = (Array.isArray(runs) ? runs : []).filter((r: AgentRun) => inMonth(r.started_at)).length;
            const rows: any[] = Array.isArray(usage?.logs) ? usage.logs : [];
            const monthRows = rows.filter(r => inMonth(r.timestamp));
            const cost = monthRows.reduce((sum, r) => sum + (Number(r.estimated_cost) || 0), 0);
            // A full page whose oldest row is still this month may have more behind it.
            const capped = rows.length >= MONTH_CALLS && inMonth(rows[rows.length - 1]?.timestamp);
            setMonth({ id: agentId, runs: runCount, cost, capped });
        }).catch(() => { if (live) setFailed(agentId); });
        return () => { live = false; };
    }, [agentId]);

    if (failed === agentId) return <p className="text-xs text-text-faint">This month&apos;s runs and cost could not be read.</p>;
    if (!month || month.id !== agentId) return <Loading />;
    return (
        <div className="space-y-2">
            <StatStrip
                size="sm"
                label="This month"
                cells={[
                    { label: 'Runs', value: month.runs.toLocaleString() },
                    { label: 'Estimated cost', value: `${month.capped ? '≥ ' : ''}${usd(month.cost)}`, sub: month.capped ? `${MONTH_CALLS.toLocaleString()}+ calls` : undefined },
                ]}
            />
            <p className="text-2xs text-text-faint">
                Since the 1st, in your time. Costs are estimates from the rate card; the Metrics tab has everything this agent has spent.
            </p>
        </div>
    );
}

/** One place the agent works, as a link. */
type Place = { key: string; icon: any; label: string; detail?: string; href: string };

/**
 * Everywhere this agent answers or is called, as links: chat, its channels, the
 * widgets that publish it, the workflows and schedules that call it, and the
 * agents that may hand it work. The "Used by" and "Messaging Channels" tabs
 * keep the detail.
 */
export function WhereItWorks({ agentId, channels, agents, widgets }: {
    agentId: string;
    channels: any[];
    agents: any[];
    widgets: any[];
}) {
    const refs = useAgentReferences(agentId);
    if (refs === null) return <Loading />;
    const places: Place[] = [
        { key: 'chat', icon: MessageSquare, label: 'Chat', detail: 'anyone in this workspace can talk to it', href: '/' },
        ...channels.map((c: any): Place => c.platform === 'twilio_voice'
            // A phone number answers with one agent and lives on the Phone tab (VOX1c).
            ? {
                key: `ch:${c.id}`, icon: Phone, label: c.name || 'Phone number',
                href: `/channels/phone?number=${encodeURIComponent(c.id)}`,
                detail: `phone${c.voice?.number ? ` · ${c.voice.number}` : ''}${c.agent_ids?.[0] === agentId ? ' · answers its calls' : ''}`,
            }
            : {
                key: `ch:${c.id}`, icon: MessageSquare, label: c.name || 'Channel', href: '/channels',
                detail: `${c.platform}${c.agent_ids?.[0] === agentId ? ' · answers by default' : ''}`,
            }),
        ...widgets.filter((w: any) => w.agent_id === agentId).map((w: any): Place => ({
            key: `w:${w.id}`, icon: PanelTop, label: w.name || 'Untitled widget', detail: 'chat widget', href: '/widgets',
        })),
        ...refs.orchestrations.map((o: any): Place => ({
            key: `o:${o.id}`, icon: Workflow, label: o.name || o.id, href: `/workflows?id=${encodeURIComponent(o.id)}`,
            detail: `${(o.steps || []).filter((st: any) => st.agent_id === agentId).length} step(s) call this agent`,
        })),
        ...refs.schedules.map((sch: any): Place => ({
            key: `s:${sch.id}`, icon: Clock, label: sch.name || sch.id, href: '/schedules',
            detail: sch.enabled ? 'schedule, enabled' : 'schedule, paused',
        })),
        ...agents.filter((a: any) => a.id !== agentId && (a.delegate_agent_ids || []).includes(agentId)).map((a: any): Place => ({
            key: `a:${a.id}`, icon: Bot, label: a.name || a.id, detail: 'may hand work to this agent', href: `/agents?id=${encodeURIComponent(a.id)}`,
        })),
    ];
    return (
        <ul className="divide-y divide-border">
            {places.map(p => (
                <li key={p.key}>
                    <Link href={p.href} className="flex items-center gap-3 py-2 text-sm text-text hover:text-accent-ink">
                        <p.icon className="size-4 shrink-0 text-text-faint" aria-hidden />
                        <span className="min-w-0 flex-1 truncate">{p.label}</span>
                        {p.detail && <span className="hidden min-w-0 shrink truncate text-xs text-text-faint sm:inline">{p.detail}</span>}
                    </Link>
                </li>
            ))}
        </ul>
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
