'use client';
/* eslint-disable @typescript-eslint/no-explicit-any */
import React, { useState, useEffect, useCallback } from 'react';
import {
    Activity, Check, ChevronDown, Clock, Cpu, Plus, RefreshCw, Save, Shrink, Trash2, Wand2, X, Zap,
} from 'lucide-react';
import {
    Badge, BarList, Button, ConfirmDialog, controlStyles, EmptyState, ErrorNote, IconButton, Section, Select,
    Spinner, StatStrip,
} from '@/components/ui';
import { cn, focusRing } from '@/lib/cn';
import { usd } from '@/lib/money';
import { count } from '@/lib/copy';
import { HOSTED_PROVIDERS, providerLabel } from '@/lib/models';

type HostedProvider = typeof HOSTED_PROVIDERS[number];

// -------------------------------------------------------------
// Types
// -------------------------------------------------------------
interface UsageSummary {
    total_cost: number;
    total_input_tokens: number;
    total_output_tokens: number;
    total_tokens: number;
    total_requests: number;
    total_cache_read_tokens?: number;
    total_cache_write_tokens?: number;
    total_estimated_savings?: number;
    total_response_cache_hits?: number;
    by_model: ModelStat[];
    by_session: SessionStat[];
    by_schedule?: ScheduleStat[];
}

interface CacheSummary {
    total_estimated_savings: number;
    total_requests: number;
    total_response_cache_hits: number;
    response_cache_hit_rate: number;
    total_cache_read_tokens: number;
    total_cache_write_tokens: number;
    by_model: Array<{
        model: string; requests: number;
        cache_read_tokens: number; cache_write_tokens: number;
        estimated_savings: number; estimated_cost: number;
        response_cache_hits: number;
    }>;
    by_run: Array<{
        run_id: string; requests: number;
        cache_read_tokens: number; cache_write_tokens: number;
        estimated_savings: number; estimated_cost: number;
        response_cache_hits: number; last_ts?: string;
    }>;
    disk_stats?: Record<string, { entries: number; bytes: number }>;
}
interface ScheduleStat {
    run_id: string;
    schedule_id: string;
    agent_id: string;
    requests: number;
    input_tokens: number;
    output_tokens: number;
    total_tokens: number;
    estimated_cost: number;
    models_used: string[];
    agents_used: string[];
    first_ts: string;
    last_ts: string;
}
interface ModelStat {
    model: string; provider: string; requests: number;
    input_tokens: number; output_tokens: number; total_tokens: number; estimated_cost: number;
    /** Whether the rate card has this model (U62). */
    priced?: boolean;
}

/**
 * A model the rate card does not have (U62): priced $0 on purpose (F63), which
 * read as free. A local Ollama model is free, so it is not marked.
 */
export function unpriced(stat: { priced?: boolean; provider?: string }): boolean {
    return stat.priced === false && stat.provider !== 'ollama';
}
interface SessionStat {
    session_id: string; run_id?: string | null; agent_id: string;
    agents_used?: string[]; requests: number;
    input_tokens: number; output_tokens: number; total_tokens: number;
    context_chars: number; estimated_cost: number; models_used: string[];
    first_ts: string; last_ts: string; source: string;
    schedule_id?: string; // only for schedule entries
}
interface UsageLog {
    timestamp: string; model: string; provider: string;
    session_id: string; agent_id: string; source: string; run_id?: string;
    tool_name?: string | null;
    input_tokens: number; output_tokens: number; total_tokens: number;
    context_chars: number; estimated_cost: number; latency_seconds: number;
    // Cache fields (present for cache-aware LLM calls)
    cache_read_tokens?: number;
    cache_write_tokens?: number;
    estimated_savings?: number;
    response_cache_hit?: boolean;
    // Compaction-specific fields (present when event_type === "compaction")
    event_type?: string;
    stage?: string;
    chars_before?: number;
    chars_after?: number;
    reduction_pct?: number;
    archive_path?: string | null;
}
interface PricingEntry {
    provider: string; input_per_1m: number; output_per_1m: number;
}

// -------------------------------------------------------------
// Constants
// -------------------------------------------------------------
const PROVIDERS = [
    'openai', 'anthropic', 'gemini', 'grok', 'deepseek', 'bedrock', 'ollama',
    // The hosted OpenAI-style providers (PROV1, D245).
    'mistral', 'moonshot', 'qwen', 'zai', 'minimax', 'openrouter', 'groq', 'cerebras', 'together', 'fireworks', 'nvidia',
] as const;
type Provider = typeof PROVIDERS[number];

/**
 * A provider's name. Each one had its own hue for its dot, bar, chip and tab —
 * seven colours that meant nothing but "a different provider" (UX1). The
 * bars are one hue now (`BarList`) and a provider is told by its name.
 */
const PROVIDER_META: Record<Provider, { label: string }> = {
    openai: { label: 'OpenAI' },
    anthropic: { label: 'Anthropic' },
    gemini: { label: 'Gemini' },
    grok: { label: 'xAI Grok' },
    deepseek: { label: 'DeepSeek' },
    bedrock: { label: 'AWS Bedrock' },
    ollama: { label: 'Ollama (Local)' },
    ...Object.fromEntries(HOSTED_PROVIDERS.map(id => [id, { label: providerLabel(id) }])) as Record<HostedProvider, { label: string }>,
};

/** The rows of a table on this page: the runs list's look (M2b, M2e) — hairlines, no box. */
const TH = 'py-2 pr-3 text-xs font-medium text-text-faint';
const TD = 'py-2.5 pr-3 tabular-nums';

// -------------------------------------------------------------
// Helpers
// -------------------------------------------------------------
function fmtK(n: number) {
    if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}M`;
    if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
    return n.toString();
}
function fmtKB(chars: number) {
    const kb = chars / 1024;
    return kb >= 1 ? `${kb.toFixed(1)}KB` : `${chars}B`;
}
function fmtDate(ts: string) {
    if (!ts) return '—';
    try {
        return new Date(ts).toLocaleString(undefined, {
            month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
        });
    } catch { return ts; }
}
function fmtTime(ts: string) {
    if (!ts) return '—';
    try {
        return new Date(ts).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    } catch { return ts; }
}

/**
 * A cost on this page: `$0` when free, otherwise every digit `usd` keeps — a
 * model call is priced per million tokens, so a real figure under a cent is the
 * normal case and must not round to nothing (F41). `—` is "no price".
 */
function cost(n: number | null | undefined): string {
    if (n === null || n === undefined || Number.isNaN(n)) return '—';
    return n === 0 ? '$0' : usd(n);
}

// -------------------------------------------------------------
// Sub-components
// -------------------------------------------------------------
function ProviderBadge({ provider }: { provider: string }) {
    return <Badge size="sm">{provider || 'local'}</Badge>;
}

/**
 * Cache analytics — savings, then per-model reuse and the runs that saved most.
 * Hidden when there's no cache activity yet.
 */
function CacheDashboard({ summary }: { summary: CacheSummary }) {
    if (summary.total_requests === 0) return null;
    const hitRatePct = (summary.response_cache_hit_rate * 100).toFixed(1);
    const totalReuseTokens = summary.total_cache_read_tokens;
    if (summary.total_estimated_savings === 0 && totalReuseTokens === 0 && summary.total_response_cache_hits === 0) {
        return null;
    }
    const activeModels = summary.by_model.filter(m =>
        m.estimated_savings > 0 || m.cache_read_tokens > 0 || m.response_cache_hits > 0
    );
    const activeRuns = summary.by_run.filter(r =>
        r.estimated_savings > 0 || r.cache_read_tokens > 0 || r.response_cache_hits > 0
    );
    return (
        <Section variant="plain" collapsible={false} title="Cache analytics" description="What reusing earlier work saved you.">
            <div className="space-y-5">
                {/* `StatStrip` carries `m-0`, which beats a parent's `space-y` (M2e). */}
                <div>
                    <StatStrip
                        size="sm"
                        label="Cache analytics"
                        cells={[
                            { label: 'Saved', value: cost(summary.total_estimated_savings), sub: 'vs. full-rate input', dot: 'success' },
                            { label: 'Cache reads', value: fmtK(summary.total_cache_read_tokens), sub: 'reused prefix tokens' },
                            { label: 'Cache writes', value: fmtK(summary.total_cache_write_tokens), sub: 'prefix tokens written' },
                            {
                                label: 'Response hits',
                                value: `${summary.total_response_cache_hits} / ${summary.total_requests}`,
                                sub: `${hitRatePct}% short-circuit rate`,
                            },
                        ]}
                    />
                </div>

                {(activeModels.length > 0 || activeRuns.length > 0) && (
                    <div className="grid gap-6 lg:grid-cols-2">
                        {activeModels.length > 0 && (
                            <table className="w-full text-sm">
                                <thead>
                                    <tr className="border-b border-border text-left">
                                        <th className={cn(TH, 'w-[45%]')}>Model</th>
                                        <th className={cn(TH, 'text-right')}>Cache reads</th>
                                        <th className={cn(TH, 'text-right')}>Resp hits</th>
                                        <th className={cn(TH, 'pr-0 text-right')}>Saved</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {activeModels.slice(0, 8).map(m => (
                                        <tr key={m.model} className="border-b border-border last:border-0">
                                            <td className={cn(TD, 'max-w-0 truncate text-text')} title={m.model}>{m.model}</td>
                                            <td className={cn(TD, 'text-right text-text-muted')}>{fmtK(m.cache_read_tokens)}</td>
                                            <td className={cn(TD, 'text-right text-text-muted')}>{m.response_cache_hits}/{m.requests}</td>
                                            <td className={cn(TD, 'pr-0 text-right text-text')}>{cost(m.estimated_savings)}</td>
                                        </tr>
                                    ))}
                                </tbody>
                            </table>
                        )}
                        {activeRuns.length > 0 && (
                            <table className="w-full text-sm">
                                <thead>
                                    <tr className="border-b border-border text-left">
                                        <th className={cn(TH, 'w-[55%]')}>Top workflow runs by savings</th>
                                        <th className={cn(TH, 'text-right')}>Cache reads</th>
                                        <th className={cn(TH, 'pr-0 text-right')}>Saved</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {activeRuns.slice(0, 5).map(r => (
                                        <tr key={r.run_id} className="border-b border-border last:border-0">
                                            <td className={cn(TD, 'max-w-0 truncate font-code text-xs text-text-muted')} title={r.run_id}>{r.run_id}</td>
                                            <td className={cn(TD, 'text-right text-text-muted')}>{fmtK(r.cache_read_tokens)}</td>
                                            <td className={cn(TD, 'pr-0 text-right text-text')}>{cost(r.estimated_savings)}</td>
                                        </tr>
                                    ))}
                                </tbody>
                            </table>
                        )}
                    </div>
                )}
            </div>
        </Section>
    );
}

/** What a usage row is: the glyph and word the legend explains. */
type SessionKind = 'system' | 'schedule' | 'orch' | 'chat';
const KIND: Record<SessionKind, { icon: typeof Zap; word: string; legend: string }> = {
    chat: { icon: Zap, word: 'chat', legend: 'Chat' },
    orch: { icon: Activity, word: 'orch', legend: 'Workflow' },
    schedule: { icon: Clock, word: 'schedule', legend: 'Schedule' },
    system: { icon: Wand2, word: 'system', legend: 'System prompt' },
};

/** The session rows' grid: kind, the session, then the numbers (Turns and Context from `md`/`lg`), and the chevron. */
const SESSION_COLS = cn(
    'grid items-center gap-x-4 px-2.5',
    'grid-cols-[1.25rem_minmax(0,1fr)_4.5rem_5rem_1rem]',
    'md:grid-cols-[1.25rem_minmax(0,1fr)_3.5rem_4.5rem_5rem_1rem]',
    'lg:grid-cols-[1.25rem_minmax(0,1fr)_3.5rem_4.5rem_4.5rem_5rem_1rem]',
);

// Per-session drill-down with per-call context delta
function SessionRow({ s }: { s: SessionStat }) {
    const [open, setOpen] = useState(false);
    const [logs, setLogs] = useState<UsageLog[]>([]);
    const [loadingLogs, setLoadingLogs] = useState(false);
    const isOrch = s.source === 'orchestration' || s.source?.startsWith('orchestration:');
    const isSched = s.source === 'schedule';
    const isSysprompt = s.session_id === 'unknown' && !isSched;
    const kind: SessionKind = isSysprompt ? 'system' : isSched ? 'schedule' : isOrch ? 'orch' : 'chat';
    const Icon = KIND[kind].icon;
    const name = isSysprompt ? 'System prompt generation' : (isOrch || isSched) && s.run_id ? s.run_id : s.session_id;
    const agents = (isOrch || isSched) ? (s.agents_used?.length ?? 0) : 0;

    const loadLogs = useCallback(async () => {
        if (logs.length > 0) return;
        setLoadingLogs(true);
        try {
            // Orchestration + Schedule runs: fetch by run_id to get all sub-agent calls
            const param = ((isOrch || isSched) && s.run_id)
                ? `run_id=${encodeURIComponent(s.run_id)}`
                : `session_id=${encodeURIComponent(s.session_id)}`;
            const res = await fetch(`/api/usage/logs?${param}&limit=500`);
            const data = await res.json();
            setLogs(data.logs ?? []);
        } catch { /* silently ignore */ }
        finally { setLoadingLogs(false); }
    }, [s.session_id, s.run_id, isOrch, isSched, logs.length]);

    const toggle = () => {
        if (!open) loadLogs();
        setOpen(o => !o);
    };

    // For orchestration/schedule: flat list sorted by timestamp -- agent change dividers are rendered inline in the table
    const flatOrchLogs: (UsageLog & { globalIdx: number })[] = (isOrch || isSched) && logs.length > 0
        ? [...logs]
            .sort((a, b) => a.timestamp.localeCompare(b.timestamp))
            .map((log, i) => ({ ...log, globalIdx: i }))
        : [];

    return (
        <div role="rowgroup" data-usage-session className="border-b border-border">
            <div
                role="row"
                className={cn(
                    SESSION_COLS,
                    'relative min-h-[50px] transition-colors hover:bg-row-hover',
                    open && 'bg-row-hover',
                    // The row draws the focus ring for its stretched control, as the runs list does.
                    'has-[[data-row-open]:focus-visible]:outline has-[[data-row-open]:focus-visible]:outline-2',
                    'has-[[data-row-open]:focus-visible]:-outline-offset-2 has-[[data-row-open]:focus-visible]:outline-accent',
                )}
            >
                <span role="cell" className="flex items-center">
                    <Icon className="size-3.5 text-text-faint" aria-label={KIND[kind].legend} />
                </span>
                <span role="cell" className="min-w-0 py-2">
                    <button
                        type="button"
                        data-row-open
                        data-no-focus-ring
                        onClick={toggle}
                        aria-expanded={open}
                        title={name}
                        className={cn("block w-full min-w-0 truncate text-left text-[13px] text-text after:absolute after:inset-0 after:content-['']", !isSysprompt && 'font-code')}
                    >
                        {name}
                    </button>
                    <span className="flex min-w-0 items-center gap-1.5 text-xs text-text-faint">
                        <span className="shrink-0">{KIND[kind].word}</span>
                        {agents > 0 && <span className="shrink-0">· {agents} agent{agents !== 1 ? 's' : ''}</span>}
                        {s.models_used.length > 0 && <span className="truncate">· {s.models_used.join(', ')}</span>}
                    </span>
                </span>
                <span role="cell" className="hidden text-right text-[13px] tabular-nums text-text-muted md:block">{s.requests}</span>
                <span role="cell" className="hidden text-right text-[13px] tabular-nums text-text-muted lg:block">{fmtKB(s.context_chars)}</span>
                <span role="cell" className="text-right text-[13px] tabular-nums text-text-muted">{fmtK(s.total_tokens)}</span>
                <span role="cell" className="text-right text-[13px] font-medium tabular-nums text-text">{cost(s.estimated_cost)}</span>
                <span role="cell" className="flex justify-end">
                    <ChevronDown className={cn('size-4 text-text-faint transition-transform', open && 'rotate-180')} aria-hidden />
                </span>
            </div>

            {open && (
                <div className="space-y-4 border-t border-border bg-surface-2/40 px-2.5 py-4">
                    {/* Session metadata */}
                    <dl className="m-0 grid grid-cols-2 gap-4 text-xs md:grid-cols-4">
                        <div className="min-w-0">
                            <dt className="mb-1 text-text-faint">{agents > 1 ? 'Agents' : 'Agent'}</dt>
                            <dd className="m-0">
                                {agents > 1
                                    ? <span className="flex flex-wrap gap-1">
                                        {s.agents_used!.map(a => (
                                            <Badge key={a} size="sm" className="max-w-[160px]">
                                                <Cpu className="size-2.5 shrink-0" aria-hidden /><span className="truncate font-code">{a}</span>
                                            </Badge>
                                        ))}
                                      </span>
                                    : <span className="block truncate font-code text-text-muted">{s.agent_id}</span>}
                            </dd>
                        </div>
                        <div className="min-w-0">
                            <dt className="mb-1 text-text-faint">Models</dt>
                            <dd className="m-0 flex flex-wrap gap-1">
                                {s.models_used.map(m => <Badge key={m} size="sm" className="max-w-[160px]"><span className="truncate">{m}</span></Badge>)}
                            </dd>
                        </div>
                        <div><dt className="mb-1 text-text-faint">In / Out</dt><dd className="m-0 tabular-nums text-text-muted">{fmtK(s.input_tokens)} / {fmtK(s.output_tokens)}</dd></div>
                        <div><dt className="mb-1 text-text-faint">Last active</dt><dd className="m-0 text-text-muted">{fmtDate(s.last_ts)}</dd></div>
                    </dl>

                    {/* Per-turn context breakdown */}
                    <div>
                        <p className="mb-2 text-xs font-medium text-text-muted">Per-turn breakdown</p>
                        {loadingLogs && (
                            <p className="flex items-center gap-2 py-3 text-xs text-text-faint">
                                <Spinner size={12} /> Loading turns...
                            </p>
                        )}
                        {!loadingLogs && logs.length === 0 && (
                            <p className="py-3 text-xs text-text-faint">No detailed logs found for this session.</p>
                        )}
                        {!loadingLogs && logs.length > 0 && (
                            <TurnTable
                                logs={(isOrch || isSched) ? flatOrchLogs : logs.map((l, i) => ({ ...l, globalIdx: i }))}
                                showAgentDividers={isOrch || isSched}
                            />
                        )}
                    </div>
                </div>
            )}
        </div>
    );
}

type IndexedLog = UsageLog & { globalIdx: number };

function TurnTable({ logs, showAgentDividers = false }: { logs: IndexedLog[]; showAgentDividers?: boolean }) {
    const sorted = [...logs].sort((a, b) => a.timestamp.localeCompare(b.timestamp));
    const COL_COUNT = 11; // # Time Model Tool Context DCtx In Out Latency Saved Cost
    return (
        <div className="overflow-x-auto">
            <table className="w-full text-xs">
                <thead>
                    <tr className="border-b border-border text-left">
                        <th className={cn(TH, 'pl-1')}>#</th>
                        <th className={TH}>Time</th>
                        <th className={TH}>Model</th>
                        <th className={cn(TH, 'pr-4')}>Tool</th>
                        <th className={cn(TH, 'text-right')}>Context</th>
                        <th className={cn(TH, 'text-right')}>&Delta; Context</th>
                        <th className={cn(TH, 'text-right')}>In</th>
                        <th className={cn(TH, 'text-right')}>Out</th>
                        <th className={cn(TH, 'text-right')}>Latency</th>
                        <th className={cn(TH, 'text-right')}>Saved</th>
                        <th className={cn(TH, 'text-right')}>Cost</th>
                    </tr>
                </thead>
                <tbody>
                    {(() => {
                        // Pre-compute call count + cache savings per agent segment for divider labels
                        const agentCounts: Record<string, number> = {};
                        const agentSavings: Record<string, number> = {};
                        sorted.forEach(l => {
                            const a = l.agent_id || '';
                            agentCounts[a] = (agentCounts[a] || 0) + 1;
                            agentSavings[a] = (agentSavings[a] || 0) + (l.estimated_savings || 0);
                        });
                        return sorted.map((log, i) => {
                            if (log.event_type === 'compaction') {
                                return <CompactionRow key={`compact-${log.globalIdx}`} log={log} idx={i} />;
                            }
                            const prevCtx = i > 0 ? sorted[i - 1].context_chars : 0;
                            const delta = log.context_chars - prevCtx;
                            const prevAgent = i > 0 ? (sorted[i - 1].agent_id || '') : null;
                            const currentAgent = log.agent_id || '';
                            const agentChanged = showAgentDividers && (i === 0 || currentAgent !== prevAgent);
                            const callCount = agentCounts[currentAgent] ?? 0;
                            const agentSaved = agentSavings[currentAgent] ?? 0;
                            const cacheRead = log.cache_read_tokens || 0;
                            const savings = log.estimated_savings || 0;
                            const respHit = !!log.response_cache_hit;
                            return (
                                <React.Fragment key={log.globalIdx}>
                                    {agentChanged && (
                                        <tr className="border-y border-border bg-surface-2">
                                            <td colSpan={COL_COUNT - 2} className="px-1 py-2">
                                                <span className="flex items-center gap-2">
                                                    <Cpu className="size-3 shrink-0 text-text-faint" aria-hidden />
                                                    <span className="max-w-[180px] truncate font-code text-xs font-medium text-text" title={currentAgent}>
                                                        {currentAgent}
                                                    </span>
                                                </span>
                                            </td>
                                            <td className="py-2 pr-3 text-right text-xs font-medium tabular-nums text-text-muted">
                                                {agentSaved > 0 ? usd(agentSaved) : ''}
                                            </td>
                                            <td className="py-2 pr-3 text-right text-xs font-medium tabular-nums text-text-muted">
                                                {callCount} call{callCount !== 1 ? 's' : ''}
                                            </td>
                                        </tr>
                                    )}
                                    <tr className="border-b border-border last:border-0">
                                        <td className={cn(TD, 'pl-1 text-text-faint')}>{i + 1}</td>
                                        <td className={cn(TD, 'whitespace-nowrap text-text-faint')}>{fmtTime(log.timestamp)}</td>
                                        <td className={cn(TD, 'max-w-[140px]')}>
                                            <span className="block truncate text-text" title={log.model}>{log.model}</span>
                                        </td>
                                        <td className={cn(TD, 'max-w-[130px] pr-4')}>
                                            {log.tool_name
                                                ? <span className="block truncate font-code text-text-muted" title={log.tool_name}>{log.tool_name}</span>
                                                : <span className="text-text-faint">—</span>}
                                        </td>
                                        <td className={cn(TD, 'text-right text-text-muted')}>{fmtKB(log.context_chars)}</td>
                                        <td className={cn(TD, 'text-right')}>
                                            <span className={delta > 0 ? 'font-medium text-text' : 'text-text-faint'}>
                                                {delta > 0 ? `+${fmtKB(delta)}` : '—'}
                                            </span>
                                        </td>
                                        <td className={cn(TD, 'text-right text-text-muted')}>{fmtK(log.input_tokens)}</td>
                                        <td className={cn(TD, 'text-right text-text-muted')}>{fmtK(log.output_tokens)}</td>
                                        <td className={cn(TD, 'text-right text-text-faint')}>{log.latency_seconds.toFixed(1)}s</td>
                                        <td className={cn(TD, 'text-right')}>
                                            {respHit ? (
                                                <span className="inline-flex items-center gap-1 font-medium text-text" title="Response cache hit — LLM call skipped">
                                                    <Zap className="size-3 text-machine" aria-hidden />hit
                                                </span>
                                            ) : savings > 0 || cacheRead > 0 ? (
                                                <span
                                                    className="font-medium text-text"
                                                    title={cacheRead > 0 ? `${fmtK(cacheRead)} cached tokens reused` : undefined}
                                                >
                                                    {usd(savings)}
                                                </span>
                                            ) : (
                                                <span className="text-text-faint">—</span>
                                            )}
                                        </td>
                                        <td className={cn(TD, 'text-right font-medium text-text')}>{cost(log.estimated_cost)}</td>
                                    </tr>
                                </React.Fragment>
                            );
                        });
                    })()}
                </tbody>
            </table>
        </div>
    );
}

function CompactionRow({ log, idx }: { log: IndexedLog; idx: number }) {
    // `refs`: older tool results moved to the vault, their keys left in place (D339).
    const stage = log.stage === 'trim' ? 'Trim' : log.stage === 'llm_summary' ? 'LLM summary'
        : log.stage === 'refs' ? 'Moved to the vault' : (log.stage ?? '');
    const reductionPct = log.reduction_pct ?? 0;
    const charsBefore = log.chars_before ?? 0;
    const charsAfter = log.chars_after ?? 0;
    return (
        <tr className="border-b border-border bg-machine-subtle">
            <td className={cn(TD, 'pl-1 text-text-faint')}>{idx + 1}</td>
            <td className={cn(TD, 'whitespace-nowrap text-text-faint')}>{fmtTime(log.timestamp)}</td>
            <td colSpan={2} className={cn(TD, 'pr-4')}>
                <span className="inline-flex items-center gap-1.5 text-xs font-medium text-text">
                    <Shrink className="size-3 text-machine" aria-hidden />
                    context compact
                    <Badge size="sm" tone="machine">{stage}</Badge>
                </span>
            </td>
            <td className={cn(TD, 'text-right text-text-faint')}>{fmtKB(charsAfter)}</td>
            <td className={cn(TD, 'text-right font-medium text-text')}>-{fmtKB(charsBefore - charsAfter)}</td>
            <td colSpan={3} className={cn(TD, 'text-right font-medium text-text')}>-{reductionPct}%</td>
            <td className={cn(TD, 'text-right text-text-faint')}>—</td>
            <td className={cn(TD, 'text-right text-text-faint')}>—</td>
        </tr>
    );
}

// -------------------------------------------------------------
// Editable Pricing Table
// -------------------------------------------------------------
type EditablePricing = Record<string, PricingEntry & { _editing?: boolean; _new?: boolean }>;

/** A price field: the kit's control look at the table's size. */
const PRICE_INPUT = cn(controlStyles({ size: 'sm' }), 'w-24 text-right tabular-nums');

function PricingEditor({ initialPricing, onSaved }: {
    initialPricing: Record<string, PricingEntry>;
    onSaved: (p: Record<string, PricingEntry>) => void;
}) {
    const [pricing, setPricing] = useState<EditablePricing>(() => JSON.parse(JSON.stringify(initialPricing)));
    const [activeProvider, setActiveProvider] = useState<Provider | 'all'>('all');
    const [saving, setSaving] = useState(false);
    const [saveMsg, setSaveMsg] = useState<string | null>(null);
    const [dirty, setDirty] = useState(false);
    const [availableModels, setAvailableModels] = useState<Partial<Record<Provider, string[]>>>({});

    useEffect(() => {
        fetch('/api/models')
            .then(r => r.ok ? r.json() : null)
            .then(data => {
                if (!data?.providers) return;
                const map: Partial<Record<Provider, string[]>> = {};
                for (const [provider, info] of Object.entries(data.providers) as [Provider, any][]) {
                    if (Array.isArray(info?.models)) map[provider] = info.models;
                }
                setAvailableModels(map);
            })
            .catch(() => { /* silently ignore, dropdown will show custom option only */ });
    }, []);

    // New model form state
    const [addingFor, setAddingFor] = useState<Provider | null>(null);
    const [newModel, setNewModel] = useState('');
    const [newIn, setNewIn] = useState('');
    const [newOut, setNewOut] = useState('');
    const [useCustomModel, setUseCustomModel] = useState(false);

    const openAddForm = (provider: Provider) => {
        setAddingFor(addingFor === provider ? null : provider);
        setNewModel(''); setNewIn(''); setNewOut('');
        setUseCustomModel(false);
    };

    // Update a field
    const update = (model: string, field: 'input_per_1m' | 'output_per_1m', val: string) => {
        const num = parseFloat(val);
        if (isNaN(num) || num < 0) return;
        setPricing(p => ({ ...p, [model]: { ...p[model], [field]: num } }));
        setDirty(true);
    };

    const removeModel = (model: string) => {
        setPricing(p => { const c = { ...p }; delete c[model]; return c; });
        setDirty(true);
    };

    const addModel = (provider: Provider) => {
        if (!newModel.trim()) return;
        const key = newModel.trim();
        const inp = parseFloat(newIn) || 0;
        const out = parseFloat(newOut) || 0;
        setPricing(p => ({ ...p, [key]: { provider, input_per_1m: inp, output_per_1m: out, _new: true } }));
        setNewModel(''); setNewIn(''); setNewOut('');
        setAddingFor(null);
        setUseCustomModel(false);
        setDirty(true);
    };

    const save = async () => {
        setSaving(true);
        setSaveMsg(null);
        try {
            // Strip internal UI flags
            const clean: Record<string, PricingEntry> = {};
            for (const [k, v] of Object.entries(pricing)) {
                // eslint-disable-next-line @typescript-eslint/no-unused-vars
                const { _editing, _new, ...rest } = v as any;
                clean[k] = rest;
            }
            const res = await fetch('/api/usage/pricing', {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(clean),
            });
            if (!res.ok) throw new Error(`Server error ${res.status}`);
            setSaveMsg('Saved!');
            setDirty(false);
            onSaved(clean);
            setTimeout(() => setSaveMsg(null), 2000);
        } catch (e: any) {
            setSaveMsg(`Error: ${e.message}`);
        } finally {
            setSaving(false);
        }
    };

    const providerModels = (provider: Provider | 'all') =>
        Object.entries(pricing).filter(([, v]) => provider === 'all' || v.provider === provider);

    const providerList = PROVIDERS.filter(p => p !== 'ollama' || Object.values(pricing).some(v => v.provider === 'ollama'));
    const ROW = 'grid grid-cols-[minmax(0,1fr)_7.5rem_7.5rem_2rem] items-center gap-2 sm:grid-cols-[minmax(0,1fr)_9rem_9rem_2rem]';

    return (
        <Section
            id="pricing"
            variant="plain"
            collapsible={false}
            className="scroll-mt-4"
            title="Prices used for estimates"
            description="Edit rates used for cost calculation (USD / 1M tokens)."
            actions={<>
                {saveMsg && (
                    <span role="status" className={cn('text-xs', saveMsg.startsWith('Error') ? 'text-danger' : 'text-text-muted')}>
                        {saveMsg}
                    </span>
                )}
                {dirty && (
                    <Button size="sm" onClick={save} loading={saving}>
                        {!saving && <Save className="size-3.5" aria-hidden />}
                        Save pricing
                    </Button>
                )}
            </>}
        >
            {/* Provider pills */}
            <div role="group" aria-label="Providers" className="-mx-1 mb-3 flex gap-1 overflow-x-auto px-1 pb-1">
                {(['all', ...providerList] as (typeof providerList[number] | 'all')[]).map(p => {
                    const isActive = activeProvider === p;
                    return (
                        <button key={p} type="button" onClick={() => setActiveProvider(p)}
                            aria-pressed={isActive}
                            className={cn(
                                'shrink-0 whitespace-nowrap rounded-full border px-3 py-1 text-xs font-medium transition-colors',
                                focusRing,
                                isActive
                                    ? 'border-border-strong bg-surface-2 text-text'
                                    : 'border-transparent text-text-muted hover:bg-surface-2 hover:text-text',
                            )}>
                            {p === 'all' ? 'All providers' : PROVIDER_META[p].label}
                        </button>
                    );
                })}
            </div>

            {/* Model rows */}
            <div>
                {(activeProvider === 'all' ? providerList : [activeProvider]).map(provider => {
                    const models = providerModels(provider);
                    // When a specific provider tab is selected, always show the header so the user can add models even if none exist yet
                    if (models.length === 0 && addingFor !== provider && activeProvider === 'all') return null;
                    const meta = PROVIDER_META[provider];
                    return (
                        <div key={provider} className="mb-4 last:mb-0">
                            {/* Provider header row */}
                            <div className="flex items-center justify-between border-b border-border py-2">
                                <div className="flex items-baseline gap-2">
                                    <span className="text-sm font-medium text-text">{meta.label}</span>
                                    <span className="text-xs text-text-faint">({count(models.length, 'model')})</span>
                                </div>
                                <Button variant="ghost" size="sm" onClick={() => openAddForm(provider)}>
                                    <Plus className="size-3.5" aria-hidden /> Add model
                                </Button>
                            </div>

                            {/* Column headers */}
                            {models.length > 0 && (
                                <div className={cn(ROW, 'border-b border-border py-1.5 text-xs text-text-faint')}>
                                    <span>Model ID</span>
                                    <span className="text-right">Input / 1M tokens</span>
                                    <span className="text-right">Output / 1M tokens</span>
                                    <span />
                                </div>
                            )}

                            {/* Model rows */}
                            {models.map(([model, entry]) => (
                                <div key={model} className={cn(ROW, 'border-b border-border py-2 last:border-0')}>
                                    <span className="truncate pr-2 font-code text-xs text-text" title={model}>
                                        {model}
                                        {(entry as any)._new && <Badge size="sm" tone="machine" className="ml-2">New</Badge>}
                                    </span>
                                    <div className="flex items-center justify-end gap-1">
                                        <span className="text-xs text-text-faint">$</span>
                                        <input
                                            type="number" min="0" step="0.001"
                                            aria-label={`${model} input price per million tokens`}
                                            value={entry.input_per_1m}
                                            onChange={e => update(model, 'input_per_1m', e.target.value)}
                                            className={PRICE_INPUT}
                                        />
                                    </div>
                                    <div className="flex items-center justify-end gap-1">
                                        <span className="text-xs text-text-faint">$</span>
                                        <input
                                            type="number" min="0" step="0.001"
                                            aria-label={`${model} output price per million tokens`}
                                            value={entry.output_per_1m}
                                            onChange={e => update(model, 'output_per_1m', e.target.value)}
                                            className={PRICE_INPUT}
                                        />
                                    </div>
                                    <IconButton size="sm" icon={X} label={`Remove ${model} pricing`} className="hover:text-danger"
                                        onClick={() => removeModel(model)} />
                                </div>
                            ))}

                            {/* Add model form */}
                            {addingFor === provider && (
                                <div className={cn(ROW, 'border-t border-border bg-surface-2/50 px-1 py-2.5')}>
                                    {useCustomModel ? (
                                        <input
                                            autoFocus
                                            aria-label="New model id"
                                            placeholder="model-id (e.g. my-custom-model)"
                                            value={newModel}
                                            onChange={e => setNewModel(e.target.value)}
                                            onKeyDown={e => { if (e.key === 'Escape') { setUseCustomModel(false); setNewModel(''); } }}
                                            className={controlStyles({ size: 'sm' })}
                                        />
                                    ) : (
                                        <Select
                                            value={newModel || undefined}
                                            onChange={v => {
                                                if (v === '__custom__') {
                                                    setUseCustomModel(true);
                                                    setNewModel('');
                                                } else {
                                                    setNewModel(v);
                                                }
                                            }}
                                            placeholder="Select model…"
                                            aria-label="Model"
                                            size="sm"
                                            className="w-full max-w-56"
                                            options={[
                                                ...(availableModels[provider] ?? [])
                                                    .filter((m: string) => !pricing[m])
                                                    .map((m: string) => ({ value: m, label: m })),
                                                { value: '__custom__', label: 'Custom model ID…' },
                                            ]}
                                        />
                                    )}
                                    <div className="flex items-center justify-end gap-1">
                                        <span className="text-xs text-text-faint">$</span>
                                        <input aria-label="New model input price per million tokens" placeholder="0.000" value={newIn} onChange={e => setNewIn(e.target.value)}
                                            type="number" min="0" step="0.001"
                                            className={PRICE_INPUT}
                                        />
                                    </div>
                                    <div className="flex items-center justify-end gap-1">
                                        <span className="text-xs text-text-faint">$</span>
                                        <input aria-label="New model output price per million tokens" placeholder="0.000" value={newOut} onChange={e => setNewOut(e.target.value)}
                                            type="number" min="0" step="0.001"
                                            className={PRICE_INPUT}
                                        />
                                    </div>
                                    <IconButton size="sm" icon={Check} label={`Add ${meta.label} model`}
                                        className="text-accent-ink" onClick={() => addModel(provider)} />
                                </div>
                            )}
                        </div>
                    );
                })}
            </div>
        </Section>
    );
}

// -------------------------------------------------------------
// Main Component
// -------------------------------------------------------------
export function UsageTab() {
    const [summary, setSummary] = useState<UsageSummary | null>(null);
    const [cacheSummary, setCacheSummary] = useState<CacheSummary | null>(null);
    const [pricing, setPricing] = useState<Record<string, PricingEntry>>({});
    const [loading, setLoading] = useState(true);
    const [clearing, setClearing] = useState(false);
    const [confirmClear, setConfirmClear] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [sessionPage, setSessionPage] = useState(0);
    const PAGE_SIZE = 15;

    // Session History filter state
    type SessionFilterType = 'all' | 'orchestrations' | 'agents' | 'schedules';
    const [sessionFilter, setSessionFilter] = useState<SessionFilterType>('all');

    const load = useCallback(async () => {
        setLoading(true);
        setError(null);
        try {
            const [sumRes, priceRes, cacheRes] = await Promise.all([
                fetch('/api/usage/summary'),
                fetch('/api/usage/pricing'),
                fetch('/api/usage/cache_summary'),
            ]);
            if (!sumRes.ok) throw new Error(`Summary fetch failed: ${sumRes.status}`);
            if (!priceRes.ok) throw new Error(`Pricing fetch failed: ${priceRes.status}`);
            const [sumData, priceData] = await Promise.all([sumRes.json(), priceRes.json()]);
            setSummary(sumData);
            setPricing(priceData);
            if (cacheRes.ok) {
                setCacheSummary(await cacheRes.json());
            }
        } catch (e: any) {
            setError(e.message || 'Failed to load usage data');
        } finally {
            setLoading(false);
        }
    }, []);

    useEffect(() => { load(); }, [load]);

    const handleClear = async () => {
        setConfirmClear(false);
        setClearing(true);
        try {
            await fetch('/api/usage/logs', { method: 'DELETE' });
            await load();
        } catch (e: any) {
            setError(e.message);
        } finally {
            setClearing(false);
        }
    };

    // Merge chat/orch sessions  + schedule runs into one unified list
    const sessions: SessionStat[] = [
        ...(summary?.by_session ?? []),
        ...(summary?.by_schedule ?? []).map(s => ({
            session_id: s.run_id,   // use run_id as the session_id key
            run_id: s.run_id,
            agent_id: s.agent_id,
            agents_used: s.agents_used,
            requests: s.requests,
            input_tokens: s.input_tokens,
            output_tokens: s.output_tokens,
            total_tokens: s.total_tokens,
            context_chars: 0,
            estimated_cost: s.estimated_cost,
            models_used: s.models_used,
            first_ts: s.first_ts,
            last_ts: s.last_ts,
            source: 'schedule',
            schedule_id: s.schedule_id,
        } as SessionStat)),
    ].sort((a, b) => (b.last_ts ?? '').localeCompare(a.last_ts ?? ''));

    // Derive filtered sessions
    const filteredSessions = sessions.filter(s => {
        if (sessionFilter === 'all') return true;
        if (sessionFilter === 'orchestrations') return s.source === 'orchestration' || s.source?.startsWith('orchestration:');
        if (sessionFilter === 'agents') return s.source !== 'orchestration' && !s.source?.startsWith('orchestration:') && s.session_id !== 'unknown' && s.source !== 'schedule';
        if (sessionFilter === 'schedules') return s.source === 'schedule';
        return true;
    });

    const finalSessions = filteredSessions;
    const pagedSessions = finalSessions.slice(0, (sessionPage + 1) * PAGE_SIZE);
    const has = !!summary && summary.total_requests > 0;

    return (
        <div className="space-y-8">
            {/* The one figure the page is about, with its two actions (UX1: one hero figure per view). */}
            <div className="flex flex-wrap items-end justify-between gap-x-6 gap-y-3">
                <div data-usage-hero className="min-w-0">
                    <p className="text-[13px] text-text-muted">Total cost</p>
                    <p className="text-[40px] font-semibold leading-[1.1] tracking-[-0.035em] tabular-nums text-text">
                        {summary ? cost(summary.total_cost) : loading ? <span className="inline-block h-10 w-40 animate-pulse rounded-md bg-surface-2 align-middle" /> : '—'}
                    </p>
                    <p className="mt-1 text-sm text-text-muted">
                        Estimated USD across every model call so far, at the rates under Prices used for estimates.
                    </p>
                </div>
                <div className="flex items-center gap-2">
                    <Button variant="secondary" size="sm" onClick={load} disabled={loading}>
                        <RefreshCw className={cn('size-3.5', loading && 'animate-spin')} aria-hidden />
                        Refresh
                    </Button>
                    <Button variant="ghost" size="sm" className="text-danger hover:bg-danger-subtle hover:text-danger"
                        onClick={() => setConfirmClear(true)} disabled={clearing || loading}>
                        <Trash2 className="size-3.5" aria-hidden />
                        Clear logs
                    </Button>
                </div>
            </div>

            {error && <ErrorNote>{error}</ErrorNote>}

            {!loading && summary && summary.total_requests === 0 && (
                <EmptyState title="Nothing spent yet">Costs show here from the first time an agent uses a model, in chat or in a run.</EmptyState>
            )}

            {has && (
                <>
                    {/* `StatStrip` carries `m-0`, which beats a parent's `space-y` (M2e). */}
                    <div>
                        <StatStrip
                            label="Usage so far"
                            cells={[
                                { label: 'Model calls', value: summary!.total_requests.toLocaleString() },
                                { label: 'Words in', value: fmtK(summary!.total_input_tokens), sub: 'input tokens' },
                                { label: 'Words out', value: fmtK(summary!.total_output_tokens), sub: 'output tokens' },
                            ]}
                        />
                    </div>

                    {/* Cost by model: one hue, the figure written at the end of each bar. */}
                    <Section variant="plain" collapsible={false} title="Cost by model" description="Sorted by spend.">
                        <BarList
                            label="Cost by model"
                            rows={summary!.by_model.map(stat => ({
                                key: stat.model,
                                label: <span title={stat.model}>{stat.model}</span>,
                                sub: <span className="flex min-w-0 items-center gap-1.5"><ProviderBadge provider={stat.provider} /><span className="truncate">{count(stat.requests, 'request')} · {fmtK(stat.total_tokens)} tokens</span></span>,
                                value: stat.estimated_cost,
                                display: unpriced(stat) ? (
                                    <span className="font-medium text-person-ink" title="Not on the rate card, so its spend reads $0">
                                        No price · <a href="#pricing" className="underline underline-offset-2 hover:text-text">add one</a>
                                    </span>
                                ) : cost(stat.estimated_cost),
                            }))}
                        />
                    </Section>

                    {cacheSummary && <CacheDashboard summary={cacheSummary} />}

                    {/* Session history -- always visible, includes schedule info */}
                    <Section
                        variant="plain"
                        collapsible={false}
                        title="Session history"
                        description={`${sessions.length} sessions — click to expand per-turn breakdown`}
                        actions={
                            <ul aria-label="Legend" className="m-0 flex list-none flex-wrap items-center gap-3 p-0 text-xs text-text-faint">
                                {(['chat', 'orch', 'schedule', 'system'] as SessionKind[]).map(k => {
                                    const I = KIND[k].icon;
                                    return <li key={k} className="flex items-center gap-1.5"><I className="size-3" aria-hidden /> {KIND[k].legend}</li>;
                                })}
                            </ul>
                        }
                    >
                        {/* Filter bar */}
                        <div className="mb-2 flex items-center gap-3">
                            <span className="shrink-0 text-xs text-text-faint">Filter by</span>
                            <Select
                                value={sessionFilter}
                                onChange={v => {
                                    setSessionFilter(v as SessionFilterType);
                                    setSessionPage(0);
                                }}
                                aria-label="Session filter"
                                size="sm"
                                className="w-44"
                                options={[
                                    { value: 'all', label: 'All sessions' },
                                    { value: 'orchestrations', label: 'Workflows' },
                                    { value: 'agents', label: 'Agents' },
                                    { value: 'schedules', label: 'Schedules' },
                                ]}
                            />
                            <span className="ml-auto text-xs text-text-faint">
                                {finalSessions.length} result{finalSessions.length !== 1 ? 's' : ''}
                            </span>
                        </div>

                        {pagedSessions.length === 0 ? (
                            <p className="py-8 text-center text-xs text-text-faint">No sessions match the selected filter</p>
                        ) : (
                            <div role="table" aria-label="Session history">
                                <div role="row" className={cn(SESSION_COLS, 'h-8 border-b border-border text-xs font-medium text-text-faint')}>
                                    <span role="columnheader" aria-label="Kind" />
                                    <span role="columnheader">Session</span>
                                    <span role="columnheader" className="hidden text-right md:block">Turns</span>
                                    <span role="columnheader" className="hidden text-right lg:block">Context</span>
                                    <span role="columnheader" className="text-right">Tokens</span>
                                    <span role="columnheader" className="text-right">Cost</span>
                                    <span role="columnheader" aria-label="Open" />
                                </div>
                                {pagedSessions.map((s, i) => <SessionRow key={`${s.session_id}-${s.run_id || ''}-${i}`} s={s} />)}
                            </div>
                        )}

                        {(sessionPage + 1) * PAGE_SIZE < finalSessions.length && (
                            <div className="mt-4 flex justify-center">
                                <Button variant="secondary" size="sm" onClick={() => setSessionPage(p => p + 1)}>
                                    Load more ({finalSessions.length - (sessionPage + 1) * PAGE_SIZE} remaining)
                                </Button>
                            </div>
                        )}
                    </Section>
                </>
            )}

            {/* Editable pricing -- always visible. `#pricing` is where an
                unpriced model's "add one" leads (U62). */}
            {Object.keys(pricing).length > 0 && (
                <PricingEditor
                    initialPricing={pricing}
                    onSaved={(next) => {
                        setPricing(next);
                        // A price just added clears that model's "No price" (U62).
                        fetch('/api/usage/summary').then(r => (r.ok ? r.json() : null))
                            .then(s => { if (s) setSummary(s); }).catch(() => {});
                    }}
                />
            )}

            {/* Disclaimer */}
            <div className="border-t border-border pt-5 text-xs leading-relaxed text-text-faint">
                <p className="mb-1 font-medium text-text-muted">About cost estimates</p>
                <p className="max-w-[72ch]">
                    Token counts are sourced from API response metadata for OpenAI, Anthropic, Gemini, Grok, DeepSeek,
                    and the hosted providers (Mistral, Groq, OpenRouter and the rest).
                    Ollama uses its <code className="font-code">eval_count</code> field. AWS Bedrock does not expose token counts directly,
                    so tokens are estimated from character length (chars ÷ 4) — costs for Bedrock calls may be less accurate.
                    Edit prices above and click <strong>Save pricing</strong> to update rates used for future cost calculations.
                </p>
            </div>

            <ConfirmDialog
                open={confirmClear}
                onClose={() => setConfirmClear(false)}
                onConfirm={handleClear}
                title="Delete all usage logs?"
                description="This cannot be undone."
                confirmLabel="Clear logs"
                busy={clearing}
            />
        </div>
    );
}
