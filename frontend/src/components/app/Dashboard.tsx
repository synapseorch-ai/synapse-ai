"use client";
/* eslint-disable @typescript-eslint/no-explicit-any */
/*
 * Home (M2a; UX1's approval-first Home): what needs the reader first, then how
 * the week went, then everything the overview always showed.
 *
 * - The head speaks to the reader: today's date, a greeting, and one line on
 *   how things stand. The menu still calls the page Home.
 * - "Needs you": every run paused at a human step, answered in place with the
 *   run page's own card (the same request, fields and refusals), and every
 *   scheduled agent's tool call waiting for approval, with the bell's request.
 * - The week as a strip of four figures and a bar per run; recent runs with
 *   their path; the next scheduled runs.
 * - Then what this page held before (D204: nothing is dropped): Active now,
 *   spend, requests, tokens and cache savings, spend by model, and the
 *   workspace's counts.
 *
 * Colour only for meaning (D202): amber a person, cyan the machine at work,
 * red a failure, success quiet. No boxes around sections: the approval card is
 * the one object on the page.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import Link from 'next/link';
import { AlertTriangle, Blocks, Bot, Clock, MessagesSquare, Workflow } from 'lucide-react';

import { AppScreen } from '@/components/app/AppScreen';
import { ConnectCard } from '@/components/connect/ConnectCard';
import { LearnCard } from '@/components/connect/LearnCard';
import { useOssConnectHost } from '@/components/settings/McpServerTab';
import { RESOURCE_SAVED_EVENT, useBuilder } from '@/components/app/BuilderProvider';
import { useModelSetup } from '@/components/onboarding/ModelSetupProvider';
import { NOTIFICATION_EVENT, useNotifications } from '@/components/notifications/NotificationProvider';
import { ToolApprovalActions } from '@/components/notifications/ToolApproval';
import {
    byHandFavourite, ComingUp, firstName, GettingStarted, greeting, HomeSection, HomeStats, MAX_CARDS, NeedsYou,
    AskLine, askSuggestions, RecentRuns, RunHostProvider, RunWeek, START_SUGGESTIONS, startDone, startSteps, statusLine, today, upcoming,
    weekRuns, weekStats, useClock, MoreToTry, moreToTry, type RunRecord, type RunRow, type ToolCall, type TryKey,
    type WaitingBrief,
} from '@/components/run';
import type { RunSummary } from '@/components/runs/summary';
import { Button, LinkButton, Mark, StatStrip, TextLink, usePersisted } from '@/components/ui';
import { usdCompact } from '@/lib/money';
import { plainText } from '@/lib/plainText';
import { OSS_RUN_HOST, ossRunRecord, ossRunRow } from '@/lib/runHost';
import type { Orchestration } from '@/types/orchestration';

/** The runs Home counts: the latest 200, the most the list route gives. */
const RUNS_READ = 200;

/** `priced`: whether the rate card has the model (U62). */
type ModelStat = { model: string; estimated_cost: number; provider?: string; priced?: boolean };

type Summary = {
    total_cost: number;
    total_tokens: number;
    total_requests: number;
    total_estimated_savings?: number;
    by_model: ModelStat[];
};

type Schedule = {
    id: string; name?: string; enabled?: boolean; next_run_at?: string | null; target_type?: string; target_id?: string;
};

const compact = (n: number) =>
    n >= 1e9 ? `${(n / 1e9).toFixed(1)}B`
        : n >= 1e6 ? `${(n / 1e6).toFixed(1)}M`
            : n >= 1e3 ? `${(n / 1e3).toFixed(1)}K`
                : String(n);

const runHref = (id: string) => `/runs/${encodeURIComponent(id)}`;
const editorHref = (id: string) => `/workflows?id=${encodeURIComponent(id)}`;

const isWaiting = (r: RunSummary) => r.status === 'paused' && Boolean(r.waiting_for_human);
/** A waiting run's record is read again only when its question may have changed. */
const keyOf = (r: RunSummary) => `${r.run_id}:${r.current_step_id ?? ''}:${r.steps_completed ?? 0}`;

export function Dashboard({ docsMcp = false }: { docsMcp?: boolean }) {
    const clock = useClock();
    const { open: openBuilder } = useBuilder();
    const modelSetup = useModelSetup();
    // "Don't show again" on the first-workflow path: this browser's, as OSS has one person.
    const [startHidden, setStartHidden] = usePersisted<'0' | '1'>('synapseHomeStartHidden', (raw) => (raw === '1' ? '1' : '0'), '0');
    const [settings, setSettings] = useState<Record<string, unknown> | null>(null);
    const { notifications } = useNotifications();
    const [runs, setRuns] = useState<RunSummary[]>([]);
    const [runsLoaded, setRunsLoaded] = useState(false);
    const [summary, setSummary] = useState<Summary | null>(null);
    const [workflows, setWorkflows] = useState<Orchestration[]>([]);
    const [agents, setAgents] = useState<{ id: string; name?: string }[]>([]);
    const [schedules, setSchedules] = useState<Schedule[]>([]);
    const [mcpCount, setMcpCount] = useState(0);
    /** What the workspace has used of what Home's "More you can do" points at (F914); absent until read. */
    const [used, setUsed] = useState<Partial<Record<TryKey, boolean>>>({});
    const [moreHidden, setMoreHidden] = usePersisted<'0' | '1'>('synapseHomeMoreHidden', (raw) => (raw === '1' ? '1' : '0'), '0');
    /** Whether any key has ever called in (F1027): Home's connect card goes once one has, or when put away. */
    const [keyUsed, setKeyUsed] = useState<boolean | undefined>(undefined);
    const [connectHidden, setConnectHidden] = usePersisted<'0' | '1'>('synapseHomeConnectHidden', (raw) => (raw === '1' ? '1' : '0'), '0');
    const connectHost = useOssConnectHost();
    const [learnHidden, setLearnHidden] = usePersisted<'0' | '1'>('synapseHomeLearnHidden', (raw) => (raw === '1' ? '1' : '0'), '0');
    const [name, setName] = useState<string | null>(null);
    const [loaded, setLoaded] = useState(false);
    /** Each waiting run's record, as its run page reads it; `null` when it could not be read. */
    const [records, setRecords] = useState<Record<string, { key: string; data: any | null }>>({});
    const reading = useRef(new Set<string>());

    const refreshRuns = useCallback(() => {
        fetch(`/api/orchestrations/runs?limit=${RUNS_READ}`)
            .then(r => r.json())
            .then(d => { if (Array.isArray(d)) setRuns(d); })
            .catch(() => { })
            .finally(() => setRunsLoaded(true));
    }, []);

    const refreshCounts = useCallback(() => {
        const j = (url: string) => fetch(url).then(r => (r.ok ? r.json() : null)).catch(() => null);
        Promise.all([
            j('/api/usage/summary'), j('/api/orchestrations'), j('/api/agents'),
            j('/api/schedules'), j('/api/mcp/servers'), j('/api/personal-details'), j('/api/settings'),
            j('/api/rubrics'), j('/api/knowledge/bases'), j('/api/settings/api-keys'), j('/api/messaging/channels'),
        ]).then(([usage, orch, agentList, scheduleList, mcp, me, set, rubrics, bases, keys, channels]) => {
            const list = (v: any) => (Array.isArray(v) ? v : Array.isArray(v?.servers) ? v.servers : []);
            if (usage) setSummary(usage);
            setWorkflows(list(orch));
            setAgents(list(agentList));
            setSchedules(list(scheduleList));
            setMcpCount(list(mcp).length);
            setName(firstName(me?.first_name));
            if (set && typeof set === 'object') setSettings(set);
            setUsed(usedOf({ rubrics, bases, channels }));
            setKeyUsed(anyKeyUsed(keys));
            setLoaded(true);
        });
    }, []);

    useEffect(() => {
        refreshRuns();
        refreshCounts();
        const interval = setInterval(refreshRuns, 10000);
        const onVisible = () => { if (document.visibilityState === 'visible') refreshRuns(); };
        // The AI builder can add an agent or a workflow from any screen, and
        // the counts are of exactly those.
        const onSaved = () => { refreshRuns(); refreshCounts(); };
        document.addEventListener('visibilitychange', onVisible);
        window.addEventListener(NOTIFICATION_EVENT, refreshRuns);
        window.addEventListener(RESOURCE_SAVED_EVENT, onSaved);
        return () => {
            clearInterval(interval);
            document.removeEventListener('visibilitychange', onVisible);
            window.removeEventListener(NOTIFICATION_EVENT, refreshRuns);
            window.removeEventListener(RESOURCE_SAVED_EVENT, onSaved);
        };
    }, [refreshRuns, refreshCounts]);

    // Oldest wait first: the one that has held a run up longest leads.
    const waiting = useMemo(
        () => runs.filter(isWaiting).sort((a, b) => String(a.started_at ?? '').localeCompare(String(b.started_at ?? ''))),
        [runs],
    );

    // Read the record of each run answered in place, once per question.
    useEffect(() => {
        for (const r of waiting.slice(0, MAX_CARDS)) {
            const key = keyOf(r);
            if (records[r.run_id]?.key === key || reading.current.has(key)) continue;
            reading.current.add(key);
            fetch(`/api/orchestrations/runs/${encodeURIComponent(r.run_id)}`)
                .then(res => (res.ok ? res.json() : null))
                .catch(() => null)
                .then(data => setRecords(cur => ({ ...cur, [r.run_id]: { key, data } })))
                .finally(() => reading.current.delete(key));
        }
    }, [waiting, records]);

    const scheduleNames = useMemo(() => schedules.map(s => ({ id: s.id, name: s.name ?? s.id })), [schedules]);
    const rows: RunRow[] = useMemo(
        () => runs.map(r => ossRunRow(r, workflows, scheduleNames)),
        [runs, workflows, scheduleNames],
    );
    const byWorkflow = useMemo(() => new Map(workflows.map(w => [w.id, w])), [workflows]);

    // Needs you: the first few runs as cards, the rest (and any whose record would not read) as rows.
    const cards: RunRecord[] = [];
    const more: WaitingBrief[] = [];
    waiting.forEach((r, i) => {
        const data = records[r.run_id]?.data;
        const record = data ? ossRunRecord(data, byWorkflow.get(r.orchestration_id) ?? null, scheduleNames) : null;
        if (i < MAX_CARDS && record && record.status === 'paused' && record.humanPrompt) cards.push(record);
        else if (i >= MAX_CARDS || records[r.run_id]) {
            more.push({
                id: r.run_id,
                workflowName: byWorkflow.get(r.orchestration_id)?.name ?? null,
                prompt: record?.humanPrompt ?? null,
                since: r.started_at ?? null,
            });
        }
    });
    const tools: ToolCall[] = notifications
        .filter(n => n.kind === 'tool_approval' && !n.resolved)
        .map(n => ({
            id: String(n.id),
            title: n.title,
            body: n.body ? plainText(n.body) : null,
            at: new Date(n.ts * 1000).toISOString(),
            actions: <ToolApprovalActions n={n} />,
            source: n.data?.door === 'voice' ? 'A caller on the phone' : undefined,
        }));

    const week = useMemo(() => weekRuns(rows, clock.now), [rows, clock.now]);
    const stats = weekStats(week);
    // Two hundred runs that all started this week: the week holds more than Home read.
    const capped = runs.length >= RUNS_READ && week.length === rows.length;
    const waitingCount = waiting.length + tools.length;
    const pausedSince = cards
        .map(c => [...c.history].reverse().find(h => h.status === 'paused')?.started_at)
        .concat(waiting.slice(cards.length).map(r => r.started_at ?? undefined))
        .filter((s): s is string => Boolean(s))
        .sort()[0] ?? null;
    const next = upcoming(schedules, s => targetName(s, workflows, agents));
    const favourite = byHandFavourite(week);

    // A new workspace's first workflow, as a path that ticks itself (Naveen's ask).
    const hasHuman = (w: Orchestration | undefined) => Boolean(w?.steps?.some(st => st.type === 'human'));
    const newest = [...workflows].sort((a, b) => String(b.updated_at ?? '').localeCompare(String(a.updated_at ?? '')))[0];
    const humanWorkflow = workflows.find(w => hasHuman(w));
    const latest = [...rows].sort((a, b) => String(b.startedAt ?? '').localeCompare(String(a.startedAt ?? '')))[0];
    const steps = startSteps({
        // The backend's one readiness (ONB2, F728): this read `settings[key]`, which is served blank
        // (F201), so a saved key never ticked the step, and Ollama or a model CLI never did.
        modelReady: modelSetup.ready === true,
        // The step's own read moves when the first-run step saves; settings are read once.
        modelText: (() => {
            const model = modelSetup.model ?? (typeof settings?.model === 'string' ? settings.model : '');
            return model ? `Using ${model}` : undefined;
        })(),
        workflow: newest ? { id: newest.id, name: newest.name } : null,
        lastRun: latest ? { at: latest.startedAt, name: latest.workflowName } : null,
        humanWorkflow: humanWorkflow ? { id: humanWorkflow.id, name: humanWorkflow.name } : null,
        answered: rows.some(r => r.status === 'completed' && hasHuman(byWorkflow.get(r.workflowId))),
        waiting: waiting.length > 0,
        runsOnItsOwn: schedules.some(s => s.enabled !== false),
    });
    const showStart = loaded && runsLoaded && startHidden !== '1' && !startDone(steps);
    // Once a workflow has run the path folds to one line under Needs you (Naveen's call).
    const startFolded = Boolean(steps.find(st => st.id === 'run')?.done);
    const target = newest ?? null;

    const live = rows.filter(r => r.status === 'running' || r.status === 'paused');
    const recent = [...rows]
        .sort((a, b) => String(b.startedAt ?? '').localeCompare(String(a.startedAt ?? '')))
        .slice(0, 6);

    const models = (summary?.by_model ?? [])
        .slice()
        .sort((a, b) => b.estimated_cost - a.estimated_cost)
        .slice(0, 6);
    const maxCost = Math.max(...models.map(m => m.estimated_cost), 0);
    // Models the rate card does not have read "$0" as if free, and fell off the
    // top six by cost (U62). A local Ollama model is free, so it is not counted.
    const unpricedCount = (summary?.by_model ?? []).filter(m => m.priced === false && m.provider !== 'ollama').length;
    const schedulesOn = schedules.filter(s => s.enabled !== false).length;

    // What the ask line's suggestions follow (ASSIST1): what waits, the newest failure in the last day, what runs.
    const dayAgo = clock.now - 86_400_000;
    const failedToday = recent.find(r => r.status === 'failed' && Date.parse(String(r.startedAt ?? '')) >= dayAgo);
    const askFacts = {
        waiting: waitingCount,
        failedWorkflow: failedToday?.workflowName ?? null,
        running: rows.filter(r => r.status === 'running').length,
        empty: workflows.length === 0,
        hour: Number.isNaN(clock.now) ? null : Number(new Intl.DateTimeFormat('en-US', {
            hour: 'numeric', hourCycle: 'h23', timeZone: clock.timeZone }).format(clock.now)),
    };

    // Until mounted the clock has no "now": the lines are held empty, at their height, so nothing moves.
    const blank = ' ';

    return (
        <AppScreen
            id="dashboard"
            width="wide"
            eyebrow={<span suppressHydrationWarning>{today(clock) ?? blank}</span>}
            title={<span suppressHydrationWarning>{greeting(clock, name) ?? blank}</span>}
            description={runsLoaded ? statusLine({ waiting: waitingCount, runs: stats.runs, failed: stats.failed, starting: showStart }) : blank}
            actions={
                <>
                    <LinkButton href="/">
                        <MessagesSquare className="size-4" aria-hidden />
                        Open chat
                    </LinkButton>
                </>
            }
        >
            <div className="grid gap-7">
                {/* Synapse AI's ask line (ASSIST1): above Needs you, which stays first in what Home shows. */}
                {loaded && runsLoaded && (
                    <div data-tour="home-ask">
                        <AskLine suggestions={askSuggestions(askFacts)} onAsk={(text) => openBuilder({ ask: text })} />
                    </div>
                )}

                {/* With nothing waiting, a new workspace's path leads; its fourth step says what Needs you is. */}
                {(!showStart || startFolded || waitingCount > 0) && (
                    <RunHostProvider value={OSS_RUN_HOST}>
                        <div data-tour="home-needs-you">
                            <NeedsYou runs={cards} more={more} tools={tools} hrefOf={runHref} onChanged={refreshRuns} />
                        </div>
                    </RunHostProvider>
                )}

                {showStart && (
                    <div data-tour="home-start">
                    <GettingStarted
                        steps={steps}
                        foldable={startFolded}
                        onDismiss={() => setStartHidden('1')}
                        describe={{ onSubmit: text => openBuilder(text ? { prompt: text } : undefined), suggestions: START_SUGGESTIONS }}
                        actions={{
                            model: <Button size="sm" variant="primary" onClick={modelSetup.open}>Connect a model</Button>,
                            build: (
                                <>
                                    <LinkButton href="/templates" size="sm" variant="primary">Browse templates</LinkButton>
                                    <Button size="sm" variant="secondary" onClick={() => openBuilder()}>Build with AI</Button>
                                </>
                            ),
                            run: target ? <LinkButton href={editorHref(target.id)} size="sm" variant="primary">Open “{target.name}”</LinkButton> : undefined,
                            approve: waitingCount > 0
                                ? <TextLink href="#home-needs-you">Answer it under Needs you</TextLink>
                                : (humanWorkflow ?? target)
                                    ? <LinkButton href={editorHref((humanWorkflow ?? target)!.id)} size="sm">{humanWorkflow ? `Open “${humanWorkflow.name}”` : 'Add a Human step'}</LinkButton>
                                    : undefined,
                            own: (
                                <>
                                    <LinkButton href="/schedules" size="sm" variant="primary">Add a schedule</LinkButton>
                                    <LinkButton href="/channels" size="sm">Channels</LinkButton>
                                    <LinkButton href="/settings/api_keys" size="sm">API keys</LinkButton>
                                </>
                            ),
                        }}
                    />
                    </div>
                )}

                {/* F1030: a new workspace (while Getting started shows), once the docs' MCP answers, until put away. */}
                {docsMcp && showStart && learnHidden !== '1' && (
                    <LearnCard logos={{ claude: '/claude-ai-icon.svg', openai: '/chatgpt-icon.svg' }} onDismiss={() => setLearnHidden('1')} />
                )}

                {connectHost && keyUsed === false && connectHidden !== '1' && (
                    <ConnectCard host={connectHost} settingsHref="/settings/mcp_server" onDismiss={() => setConnectHidden('1')} />
                )}

                {moreHidden !== '1' && (
                    <MoreToTry
                        keys={moreToTry(
                            [
                                'evals',
                                ...(settings?.knowledge_enabled ? ['knowledge' as const] : []),
                                ...(settings?.messaging_enabled ? ['channels' as const] : []),
                            ],
                            used,
                        )}
                        onDismiss={() => setMoreHidden('1')}
                    />
                )}

                <HomeStats
                    running={rows.filter(r => r.status === 'running').length}
                    nextRun={next[0]?.at ?? null}
                    waiting={waitingCount}
                    waitingSince={pausedSince}
                    failed={stats.failed}
                    failedSub={stats.lastFailure
                        ? <Link href={runHref(stats.lastFailure.id)} className="text-accent-ink hover:underline">{stats.lastFailure.workflowName ?? 'Deleted workflow'}</Link>
                        : undefined}
                    completed={stats.completed}
                    runs={stats.runs}
                    capped={capped}
                    clock={clock}
                />

                <HomeSection id="active" title="Active now" link={live.length > 0 ? { href: '/runs', label: 'All runs' } : undefined}>
                    <RecentRuns
                        live
                        label="Active now"
                        rows={live}
                        workflows={workflows}
                        hrefOf={row => runHref(row.id)}
                        empty={
                            <p className="m-0 text-sm text-text-muted">
                                <span className="font-medium text-text">Nothing running.</span>{' '}
                                Start one from <TextLink href="/workflows">Workflows</TextLink>.
                            </p>
                        }
                    />
                </HomeSection>

                <RunWeek rows={week} hrefOf={row => runHref(row.id)} clock={clock} />

                <div className="grid gap-7 min-[1100px]:grid-cols-[minmax(0,1.5fr)_minmax(0,1fr)]">
                    <HomeSection id="recent" title="Recent runs" link={{ href: '/runs', label: 'All runs' }}>
                        <RecentRuns
                            rows={recent}
                            workflows={workflows}
                            hrefOf={row => runHref(row.id)}
                            empty={<p className="m-0 text-sm text-text-muted">No runs yet</p>}
                        />
                    </HomeSection>
                    <HomeSection id="next" title="Coming up" link={{ href: '/schedules', label: 'Schedules' }}>
                        <ComingUp
                            items={next}
                            clock={clock}
                            ways={{ schedules: '/schedules', channels: '/channels', api: '/settings/api_keys' }}
                            tip={favourite ? {
                                text: `“${favourite.name}” ran ${favourite.count} times by hand this week. Put it on a schedule?`,
                                action: (
                                    <Button size="sm" variant="secondary" onClick={() => openBuilder({ prompt: `Put the workflow “${favourite.name}” on a schedule.` })}>
                                        <Mark size={20} compact={false} className="text-accent" />
                                        Schedule it with AI
                                    </Button>
                                ),
                            } : null}
                        />
                    </HomeSection>
                </div>

                <HomeSection id="spend" title="Spend and usage" link={{ href: '/usage', label: 'Usage' }}>
                    <StatStrip
                        size="sm"
                        label="Every recorded model call"
                        cells={[
                            { label: 'Total spend', value: summary ? usdCompact(summary.total_cost) : '—', sub: 'all recorded LLM calls' },
                            { label: 'Requests', value: summary ? summary.total_requests.toLocaleString() : '—' },
                            { label: 'Tokens', value: summary ? compact(summary.total_tokens) : '—' },
                            { label: 'Cache saved', value: summary ? usdCompact(summary.total_estimated_savings ?? 0) : '—', sub: 'vs uncached pricing' },
                        ]}
                    />
                </HomeSection>

                <div className="grid gap-7 lg:grid-cols-2">
                    {/* One series, so one hue and no legend: the row label is the
                        identity and the value is direct-labelled beside it. */}
                    <HomeSection id="models" title="Spend by model" link={{ href: '/usage', label: 'Usage' }}>
                        {models.length === 0 ? (
                            <p className="m-0 text-sm text-text-muted">No usage recorded yet</p>
                        ) : (
                            <div>
                                <ul className="m-0 list-none space-y-3 p-0">
                                    {models.map(m => (
                                        <li key={m.model} title={`${m.model} — ${usdCompact(m.estimated_cost)}`}>
                                            <div className="mb-1.5 flex items-baseline justify-between gap-3">
                                                <span className="min-w-0 truncate text-sm text-text-muted">{m.model}</span>
                                                {m.priced === false && m.provider !== 'ollama' ? (
                                                    <span className="inline-flex shrink-0 items-center gap-1 text-sm font-medium text-text">
                                                        <AlertTriangle className="size-3.5" aria-hidden />
                                                        No price
                                                    </span>
                                                ) : (
                                                    <span className="shrink-0 text-sm tabular-nums text-text">
                                                        {usdCompact(m.estimated_cost)}
                                                    </span>
                                                )}
                                            </div>
                                            <div className="h-2 w-full rounded-[4px] bg-surface-2">
                                                <div
                                                    className="h-2 rounded-[4px] bg-machine"
                                                    style={{ width: `${maxCost > 0 ? Math.max(2, (m.estimated_cost / maxCost) * 100) : 0}%` }}
                                                />
                                            </div>
                                        </li>
                                    ))}
                                </ul>
                                {unpricedCount > 0 && (
                                    <p className="mb-0 mt-4 text-xs text-text-muted">
                                        {unpricedCount} {unpricedCount === 1 ? 'model has' : 'models have'} no price, so
                                        {unpricedCount === 1 ? ' its' : ' their'} spend reads $0.{' '}
                                        <TextLink href="/usage#pricing">Add one</TextLink>
                                    </p>
                                )}
                            </div>
                        )}
                    </HomeSection>

                    <HomeSection id="workspace" title="Workspace">
                        <ul className="m-0 list-none divide-y divide-border border-y border-border p-0">
                            {[
                                { icon: Workflow, label: 'Workflows', href: '/workflows', value: workflows.length },
                                { icon: Bot, label: 'Agents', href: '/agents', value: agents.length },
                                {
                                    icon: Clock, label: 'Schedules', href: '/schedules', value: schedules.length,
                                    sub: schedules.length > 0 ? `${schedulesOn} enabled` : undefined,
                                },
                                { icon: Blocks, label: 'Apps', href: '/apps', value: mcpCount },
                            ].map(row => (
                                <li key={row.href}>
                                    <Link href={row.href} className="flex items-center gap-3 px-1 py-2.5 transition-colors hover:text-text">
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
                    </HomeSection>
                </div>
            </div>
        </AppScreen>
    );
}

/**
 * What Home's "More you can do" reads as used (F914): a rubric, a knowledge base, a channel. A list that could not be
 * read stays unknown, so its card stays hidden (`moreToTry`). Using Synapse from an AI tool has its own card (F1027).
 */
function usedOf(lists: { rubrics: unknown; bases: unknown; channels: unknown }): Partial<Record<TryKey, boolean>> {
    const some = (v: unknown) => (Array.isArray(v) ? v.length > 0 : undefined);
    return { evals: some(lists.rubrics), knowledge: some(lists.bases), channels: some(lists.channels) };
}

/** Whether any API key has ever called in (Home's connect card, F1027); unknown when the list couldn't be read. */
function anyKeyUsed(keys: unknown): boolean | undefined {
    if (!Array.isArray(keys)) return undefined;
    return keys.some((k: { last_used_at?: string | null } | null) => Boolean(k?.last_used_at));
}

/** What a schedule runs, by name. */
function targetName(s: Schedule, workflows: Orchestration[], agents: { id: string; name?: string }[]): string | null {
    if (!s.target_id) return null;
    if (s.target_type === 'orchestration') return workflows.find(w => w.id === s.target_id)?.name ?? null;
    if (s.target_type === 'agent') return agents.find(a => a.id === s.target_id)?.name ?? null;
    return null;
}
