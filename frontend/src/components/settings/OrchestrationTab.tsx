'use client';
/* eslint-disable @typescript-eslint/no-explicit-any */
import { useState, useEffect, useCallback, useRef } from 'react';
import { createPortal } from 'react-dom';
import { Plus, Save, Play, Trash, Square, Copy, Check, Radio, Bot, ExternalLink, X, Sparkles, ArrowDown, ArrowLeft, Undo2, Redo2, AlertTriangle, Pause, RefreshCw, GitFork, GitBranch, GitMerge, Info, Minimize2, CornerDownRight, Wrench, Brain, MessageSquare, ListTodo, Circle, CircleDot, CircleCheck, CircleX } from 'lucide-react';
import { Button, Combobox, Hint, IconButton, Mark, Markdown, Modal, SearchInput, Spinner } from '@/components/ui';
import { matchesQuery } from '@/lib/search';
import { RESOURCE_SAVED_EVENT, useBuilder } from '@/components/app/BuilderProvider';
import { cloneStep, generateStepId, removeStepFromGraph } from '../orchestration/graph';
import { validateOrchestration } from '../orchestration/validate';
import { useBuilderShortcuts, useDraftHistory } from '../orchestration/use-draft-history';
import { readWithStallTimeout } from '@/lib/sse';
import { refusedAnswerMessage } from '@/lib/answers';
import { ReactFlowProvider } from '@xyflow/react';
import { WorkflowCanvas } from '../orchestration/WorkflowCanvas';
import { StepConfigPanel } from '../orchestration/StepConfigPanel';
import { StateSchemaEditor } from '../orchestration/StateSchemaEditor';
import { HumanFieldsForm } from '../orchestration/HumanFieldsForm';
import { humanAnswer, initialHumanValues, type HumanField, type HumanValues } from '../orchestration/humanFields';
import type { Orchestration, StepConfig } from '@/types/orchestration';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { ConfirmationModal } from './ConfirmationModal';
import { ToastNotification } from './ToastNotification';

type ToolCallLogEntry = { kind: 'tool_call'; tool_name: string; args: Record<string, any>; step_name?: string };
type ToolResultLogEntry = { kind: 'tool_result'; tool_name: string; preview: string };
type StepResultLogEntry = { kind: 'step_result'; step_name: string; step_type: 'agent' | 'llm' | 'print' | 'extract_json'; content: string };
// Model reasoning ([REASONING] blocks) and inter-tool prose, collapsible so a
// chatty agent doesn't drown the structural log lines.
type ReasoningLogEntry = { kind: 'reasoning'; step_name?: string; content: string };
type ThoughtLogEntry = { kind: 'thought'; step_name?: string; content: string };
type TaskStatus = 'pending' | 'in_progress' | 'completed' | 'cancelled';
type TasksLogEntry = { kind: 'tasks'; step_name?: string; tasks: { content: string; status: TaskStatus }[] };
// A structural log line: the tone picks an icon and a colour in the renderer,
// which is what replaced the emoji prefixes — an icon column reads as product
// chrome where a row of pictographs read as a group chat.
type LineTone = 'start' | 'ok' | 'error' | 'pause' | 'loop' | 'flow' | 'parallel' | 'merge' | 'end' | 'info' | 'warn' | 'compact' | 'branch' | 'muted';
type LineLogEntry = { kind: 'line'; tone: LineTone; text: string };
type LogEntry = string | ToolCallLogEntry | ToolResultLogEntry | StepResultLogEntry | ReasoningLogEntry | ThoughtLogEntry | TasksLogEntry | LineLogEntry;

const line = (tone: LineTone, text: string): LineLogEntry => ({ kind: 'line', tone, text });

const LINE_META: Record<LineTone, { icon: React.FC<{ size?: number; className?: string }>; cls: string }> = {
    start:    { icon: Play,            cls: 'text-blue-400' },
    ok:       { icon: Check,           cls: 'text-green-400' },
    error:    { icon: X,               cls: 'text-red-400' },
    pause:    { icon: Pause,           cls: 'text-amber-400' },
    loop:     { icon: RefreshCw,       cls: 'text-accent' },
    flow:     { icon: GitFork,         cls: 'text-zinc-300' },
    parallel: { icon: GitBranch,       cls: 'text-zinc-300' },
    merge:    { icon: GitMerge,        cls: 'text-zinc-300' },
    end:      { icon: Square,          cls: 'text-zinc-400' },
    info:     { icon: Info,            cls: 'text-zinc-400' },
    warn:     { icon: AlertTriangle,   cls: 'text-amber-400' },
    compact:  { icon: Minimize2,       cls: 'text-zinc-400' },
    branch:   { icon: CornerDownRight, cls: 'text-text-faint pl-2' },
    muted:    { icon: Info,            cls: 'text-text-faint' },
};

type RunStepStatus = 'pending' | 'running' | 'paused' | 'completed' | 'failed';

// Pure reducer: mark each step present in a persisted checkpoint's step_history
// as completed/failed. Shared by restoreRun and reattachRun.
function applyStepHistory(
    prev: Record<string, RunStepStatus>,
    stepHistory: any[],
): Record<string, RunStepStatus> {
    const next = { ...prev };
    for (const h of stepHistory) {
        if (h?.step_id) next[h.step_id] = h.status === 'failed' ? 'failed' : 'completed';
    }
    return next;
}

const EMPTY_ORCHESTRATION: Orchestration = {
    id: '',
    name: 'New Orchestration',
    description: '',
    steps: [],
    entry_step_id: '',
    state_schema: {},
    max_total_turns: 100,
    timeout_minutes: 30,
    trigger: 'manual',
    rubric_id: '',
};

/** A human step's declared fields off the wire — anything else is no fields (F75). */
function fieldList(raw: unknown): HumanField[] {
    return Array.isArray(raw) ? (raw as HumanField[]) : [];
}

export function OrchestrationTab({ initialRunId, initialOrchId }: { initialRunId?: string; initialOrchId?: string } = {}) {
    // --- Orchestration list ---
    const [orchestrations, setOrchestrations] = useState<Orchestration[]>([]);
    const [orchQuery, setOrchQuery] = useState('');
    const [selectedOrchId, setSelectedOrchId] = useState<string | null>(null);
    // The draft with undo/redo, dirty tracking and the unload guard.
    // `setDraft` records history (edits); `replaceDraft` resets it (navigation).
    const { draft, setDraft, replaceDraft, markSaved, undo, redo, canUndo, canRedo, dirty } = useDraftHistory();

    // The builder runs from the app shell now and can be talked to from any
    // screen, so this one learns about a save the way every other list does:
    // from the event, not from a callback it owned.
    const loadSavedOrchestration = useCallback(async (orch: any) => {
        setOrchestrations((prev) => {
            const idx = prev.findIndex((o) => o.id === orch.id);
            return idx >= 0 ? prev.map((o) => (o.id === orch.id ? orch : o)) : [...prev, orch];
        });
        replaceDraft(orch, { saved: true });
        setSelectedOrchId(orch.id);
        // Re-read for the canonical version, with every step resolved.
        try {
            const res = await fetch('/api/orchestrations');
            if (!res.ok) return;
            const all = await res.json();
            const freshList = Array.isArray(all) ? all : [];
            setOrchestrations(freshList);
            const fresh = freshList.find((o: any) => o.id === orch.id);
            if (fresh) replaceDraft(fresh, { saved: true });
        } catch { /* the event's copy stands */ }
    }, [replaceDraft]);

    useEffect(() => {
        const onSaved = (event: Event) => {
            const saved = (event as CustomEvent).detail;
            if (saved?.kind === 'agent' && saved.resource) {
                setAgents((prev) => {
                    const idx = prev.findIndex((a) => a.id === saved.resource.id);
                    return idx >= 0
                        ? prev.map((a) => (a.id === saved.resource.id ? saved.resource : a))
                        : [...prev, saved.resource];
                });
                return;
            }
            if (saved?.kind !== 'orchestration') return;
            if (saved.action === 'deleted') {
                setOrchestrations((prev) => prev.filter((o) => o.id !== saved.id));
                return;
            }
            if (!saved.resource) return;
            // Loading over unsaved work is the user's call, not ours. This used
            // to replace the draft silently, and the builder can now be talked
            // to from any screen, so the collision is reachable in a way it
            // was not when the panel lived inside this editor.
            if (dirty && saved.id === selectedOrchId) {
                setBuilderSavedOrch(saved.resource);
                return;
            }
            void loadSavedOrchestration(saved.resource);
        };
        window.addEventListener(RESOURCE_SAVED_EVENT, onSaved);
        return () => window.removeEventListener(RESOURCE_SAVED_EVENT, onSaved);
    }, [dirty, selectedOrchId, loadSavedOrchestration]);
    const [agents, setAgents] = useState<any[]>([]);
    const [availableModels, setAvailableModels] = useState<string[]>([]);
    const [saving, setSaving] = useState(false);
    const [toast, setToast] = useState<{ show: boolean; message: string; type: 'success' | 'warning' | 'error' } | null>(null);
    const showToast = (message: string, type: 'success' | 'warning' | 'error' = 'success') => {
        setToast({ show: true, message, type });
        setTimeout(() => setToast(null), 4000);
    };

    // --- Step selection ---
    const [selectedStepId, setSelectedStepId] = useState<string | null>(null);

    // --- Run state ---
    const [runStatus, setRunStatus] = useState<'idle' | 'running' | 'paused' | 'completed' | 'failed' | 'cancelled'>('idle');
    const [runStepStatuses, setRunStepStatuses] = useState<Record<string, 'pending' | 'running' | 'paused' | 'completed' | 'failed'>>({});
    const [runId, setRunId] = useState<string | null>(null);
    const [runInput, setRunInput] = useState('');
    const [runLog, setRunLog] = useState<LogEntry[]>([]);
    const [humanPrompt, setHumanPrompt] = useState<string | null>(null);
    const [humanContext, setHumanContext] = useState<string | null>(null);
    const [humanResponse, setHumanResponse] = useState('');
    // The step's declared fields, when it has any — answered as a form keyed by
    // name, like every other place a human step is answered (F75). Set with the
    // prompt at each place a question arrives; ignored while there is none.
    const [humanFields, setHumanFields] = useState<HumanField[]>([]);
    const [humanValues, setHumanValues] = useState<HumanValues>({});
    const abortRef = useRef<AbortController | null>(null);
    // Mirrors `runId` so the streamSSE catch / wake handler can read the live run
    // id without a stale closure. Timestamp of the last byte (incl. heartbeats)
    // tells a healthy stream from a dead one after sleep. reattachActiveRef
    // guards against overlapping reconnect loops.
    const currentRunIdRef = useRef<string | null>(null);
    const lastChunkAtRef = useRef(0);
    const reattachActiveRef = useRef(false);
    // Highest journal event id seen on the reattach stream (`id:` lines) — a
    // reconnect resumes with ?after=<this> instead of replaying everything.
    const lastEventIdRef = useRef(0);
    // One-line "what the model is doing right now" indicator for the Run Log.
    const [liveActivity, setLiveActivity] = useState<string | null>(null);
    // Map of orch_step_id -> pending step result (supports parallel branches)
    const pendingStepResultRef = useRef<Map<string, { step_name: string; step_type: 'agent' | 'llm' | 'print' | 'extract_json'; content: string }>>(new Map());
    const [responseModal, setResponseModal] = useState<{ step_name: string; step_type?: string; content: string } | null>(null);
    const [confirmDeleteOrchId, setConfirmDeleteOrchId] = useState<string | null>(null);
    const { open: openBuilder, setCurrentOrchestration } = useBuilder();
    // Set when the builder saved the orchestration being edited while the draft
    // had unsaved changes — the user chooses which version survives.
    const [builderSavedOrch, setBuilderSavedOrch] = useState<any | null>(null);

    // --- Active runs (for reconnect banner) ---
    type RunSummary = {
        run_id: string;
        orchestration_id: string;
        status: string;
        started_at?: string | null;
        ended_at?: string | null;
        current_step_id?: string | null;
        steps_completed?: number;
        last_step_name?: string | null;
        waiting_for_human?: boolean;
        total_cost_usd?: number | null;
        session_id?: string | null;
        /** The judge's latest verdict, when the orchestration has a rubric
         *  bound. `null` is "not scored", which is not the same as zero. */
        score?: number | null;
        score_regression?: boolean;
    };
    const [activeRuns, setActiveRuns] = useState<RunSummary[]>([]);
    const activeRunsPollRef = useRef<ReturnType<typeof setInterval> | null>(null);
    // Rubrics, for the "score runs against" picker in Guardrails. Names only —
    // the editor lives on /evals.
    const [rubrics, setRubrics] = useState<Array<{ id: string; name: string }>>([]);
    const [pastRuns, setPastRuns] = useState<RunSummary[]>([]);
    // Landing dashboard: which table is shown when no orchestration is open.
    const [schedules, setSchedules] = useState<Array<{ id: string; name: string }>>([]);
    const [landingTab, setLandingTab] = useState<'orchestrations' | 'active' | 'recent' | 'all'>('orchestrations');
    const [allRuns, setAllRuns] = useState<RunSummary[]>([]);
    const autoTabRef = useRef(false);

    // --- Fetch orchestrations + agents ---
    useEffect(() => {
        fetch('/api/orchestrations').then(r => r.json()).then(data => {
            setOrchestrations(Array.isArray(data) ? data : []);
        }).catch(() => {});

        fetch('/api/agents').then(r => r.json()).then(data => {
            setAgents(Array.isArray(data) ? data : []);
        }).catch(() => {});

        fetch('/api/models').then(r => r.json()).then(data => {
            setAvailableModels(data.all_available || []);
        }).catch(() => {});

        // Schedule names, for labelling schedule-triggered runs.
        fetch('/api/schedules').then(r => r.json()).then(data => {
            setSchedules(Array.isArray(data) ? data : []);
        }).catch(() => {});

        // Rubrics, for the Guardrails picker.
        fetch('/api/rubrics').then(r => r.json()).then(data => {
            setRubrics(Array.isArray(data) ? data : []);
        }).catch(() => {});
    }, []);

    // --- Poll active runs ---
    const fetchActiveRuns = useCallback(() => {
        fetch('/api/orchestrations/runs')
            .then(r => r.json())
            .then(data => {
                if (Array.isArray(data)) {
                    setActiveRuns(data.filter(r => r.status === 'running' || r.status === 'paused'));
                    setPastRuns(data);
                }
            })
            .catch(() => {});
    }, []);

    useEffect(() => {
        fetchActiveRuns();
        activeRunsPollRef.current = setInterval(fetchActiveRuns, 5000);
        return () => {
            if (activeRunsPollRef.current) clearInterval(activeRunsPollRef.current);
        };
    }, [fetchActiveRuns]);

    // Land on the Active table when something is running/paused (once).
    useEffect(() => {
        if (!autoTabRef.current && activeRuns.length > 0) {
            autoTabRef.current = true;
            setLandingTab('active');
        }
    }, [activeRuns]);

    // The All-runs table is heavier (up to 100 checkpoints) — fetch it only
    // while that tab is open, on a slower cadence than the active poll.
    useEffect(() => {
        if (landingTab !== 'all') return;
        const fetchAll = () => {
            fetch('/api/orchestrations/runs?limit=100')
                .then(r => r.json())
                .then(d => { if (Array.isArray(d)) setAllRuns(d); })
                .catch(() => {});
        };
        fetchAll();
        const interval = setInterval(fetchAll, 10000);
        return () => clearInterval(interval);
    }, [landingTab]);

    // --- Landing-table helpers ---
    const orchNameOf = (id: string) => orchestrations.find(o => o.id === id)?.name ?? id;
    const currentStepNameOf = (run: RunSummary): string | null => {
        const orch = orchestrations.find(o => o.id === run.orchestration_id);
        const byId = run.current_step_id
            ? orch?.steps.find(s => s.id === run.current_step_id)?.name
            : null;
        return byId || run.last_step_name || null;
    };
    // What kicked this run off. Derived from session_id, which the scheduler
    // sets to `schedule_<schedule_id>` and the orchestrator-agent path sets to
    // the chat session id; manual UI and API runs carry none.
    const triggerOf = (run: RunSummary): string => {
        const session = run.session_id || '';
        if (session.startsWith('schedule_')) {
            const schedId = session.slice('schedule_'.length);
            const sched = schedules.find(s => s.id === schedId);
            return sched ? `Schedule · ${sched.name}` : 'Schedule';
        }
        if (session) return 'Agent chat';
        return 'Manual';
    };

    const fmtDuration = (start?: string | null, end?: string | null): string => {
        if (!start) return '—';
        const s = new Date(start).getTime();
        const e = end ? new Date(end).getTime() : Date.now();
        const sec = Math.max(0, Math.round((e - s) / 1000));
        if (sec < 60) return `${sec}s`;
        const m = Math.floor(sec / 60);
        if (m < 60) return `${m}m ${sec % 60}s`;
        return `${Math.floor(m / 60)}h ${m % 60}m`;
    };
    const runStatusMeta = (run: RunSummary): { dot: string; label: string; text: string } => {
        switch (run.status) {
            case 'running': return { dot: 'bg-blue-400 animate-pulse', label: 'Running', text: 'text-blue-300' };
            case 'paused': return run.waiting_for_human
                ? { dot: 'bg-yellow-400', label: 'Needs input', text: 'text-yellow-300' }
                : { dot: 'bg-yellow-400', label: 'Paused', text: 'text-yellow-300' };
            case 'completed': return { dot: 'bg-green-500', label: 'Completed', text: 'text-zinc-400' };
            case 'cancelled': return { dot: 'bg-zinc-500', label: 'Cancelled', text: 'text-text-faint' };
            default: return { dot: 'bg-red-500', label: 'Failed', text: 'text-red-300' };
        }
    };

    // Colour band for a judge score. A flagged run is red whatever it scored:
    // the point of the column is to be scannable, and "this one regressed" is
    // the fact worth seeing from across the table.
    const runScoreMeta = (run: RunSummary): { text: string; title: string } => {
        if (run.score === null || run.score === undefined) {
            return { text: 'text-text-faint', title: 'Not scored' };
        }
        if (run.score_regression) {
            return { text: 'text-red-300', title: 'Scored below expectation' };
        }
        if (run.score >= 7.5) return { text: 'text-green-400', title: 'Scored well' };
        if (run.score >= 5) return { text: 'text-yellow-300', title: 'Scored moderately' };
        return { text: 'text-red-300', title: 'Scored poorly' };
    };

    const thCls = 'px-4 py-2 text-[10px] font-medium uppercase tracking-wider text-text-faint whitespace-nowrap';
    const tdCls = 'px-4 py-2.5 text-xs';

    const renderRunsTable = (rows: RunSummary[], empty: string) => (
        <div className="border border-border bg-zinc-900/60 rounded-md">
            <div className="overflow-x-auto">
                <table className="w-full text-left border-collapse">
                    <thead>
                        <tr className="border-b border-border bg-zinc-950/40">
                            <th className={thCls}>Status</th>
                            <th className={thCls}>Orchestration</th>
                            <th className={thCls}>Step</th>
                            <th className={thCls}>Trigger</th>
                            <th className={`${thCls} text-right`}>Progress</th>
                            <th className={`${thCls} text-right`}>Cost</th>
                            <th className={`${thCls} text-right`}>Score</th>
                            <th className={`${thCls} text-right`}>Started</th>
                            <th className={`${thCls} text-right`}>Duration</th>
                            <th className={thCls}></th>
                        </tr>
                    </thead>
                    <tbody>
                        {rows.length === 0 ? (
                            <tr>
                                <td colSpan={10} className="px-4 py-10 text-center text-xs text-text-faint italic">
                                    {empty}
                                </td>
                            </tr>
                        ) : rows.map(run => {
                            const meta = runStatusMeta(run);
                            const stepName = currentStepNameOf(run);
                            const isLive = run.status === 'running' || run.status === 'paused';
                            return (
                                <tr
                                    key={run.run_id}
                                    onClick={() => restoreRun(run)}
                                    className="border-b border-border last:border-b-0 hover:bg-row-hover cursor-pointer transition-colors group"
                                >
                                    <td className={`${tdCls} whitespace-nowrap`}>
                                        <span className="inline-flex items-center gap-2">
                                            <span className={`w-2 h-2 rounded-full shrink-0 ${meta.dot}`} />
                                            <span className={meta.text}>{meta.label}</span>
                                        </span>
                                    </td>
                                    <td className={`${tdCls} text-zinc-200 max-w-[220px]`}>
                                        <span className="block truncate">{orchNameOf(run.orchestration_id)}</span>
                                        <span className="block text-[10px] text-text-faint truncate">{run.run_id}</span>
                                    </td>
                                    <td className={`${tdCls} max-w-[200px]`}>
                                        {stepName ? (
                                            <span className={`block truncate ${isLive ? 'text-zinc-300' : 'text-text-faint'}`}>
                                                {stepName}
                                            </span>
                                        ) : <span className="text-text-faint">—</span>}
                                    </td>
                                    <td className={`${tdCls} text-text-faint max-w-[160px]`}>
                                        <span className="block truncate">{triggerOf(run)}</span>
                                    </td>
                                    <td className={`${tdCls} text-right text-zinc-400 whitespace-nowrap`}>
                                        {run.steps_completed ?? 0} step{(run.steps_completed ?? 0) === 1 ? '' : 's'}
                                    </td>
                                    <td className={`${tdCls} text-right text-text-faint whitespace-nowrap`}>
                                        {run.total_cost_usd ? `$${run.total_cost_usd.toFixed(4)}` : '—'}
                                    </td>
                                    <td className={`${tdCls} text-right whitespace-nowrap`}>
                                        <span
                                            className={runScoreMeta(run).text}
                                            title={runScoreMeta(run).title}
                                        >
                                            {run.score === null || run.score === undefined
                                                ? '—'
                                                : run.score.toFixed(1)}
                                        </span>
                                    </td>
                                    <td className={`${tdCls} text-right text-text-faint whitespace-nowrap`}>
                                        {run.started_at ? new Date(run.started_at).toLocaleString() : '—'}
                                    </td>
                                    <td className={`${tdCls} text-right text-text-faint whitespace-nowrap`}>
                                        {fmtDuration(run.started_at, run.ended_at)}
                                    </td>
                                    <td className={`${tdCls} text-right whitespace-nowrap`}>
                                        <span className="text-[11px] text-text-faint group-hover:text-zinc-300 transition-colors">
                                            View →
                                        </span>
                                    </td>
                                </tr>
                            );
                        })}
                    </tbody>
                </table>
            </div>
        </div>
    );

    // --- Restore a run from the active runs banner / recent runs / deep link ---
    // The run's event journal is replayable, so restoring = hydrate step
    // statuses from the checkpoint (fast canvas paint), then replay the journal
    // from the start — which rebuilds the exact same Run Log as live viewing —
    // and keep tailing live events until the run ends.
    const restoreRun = useCallback(async (runInfo: { run_id: string; orchestration_id: string; status: string }) => {
        const orch = orchestrations.find(o => o.id === runInfo.orchestration_id);
        if (!orch) {
            // No definition to render the canvas from — bail loudly rather than
            // setting draft to null, which would silently re-render the landing
            // page and look like the click did nothing. The runs API filters
            // these out, so this is a backstop (e.g. deleted mid-session).
            showToast('That run\'s orchestration no longer exists', 'error');
            return;
        }
        setSelectedOrchId(runInfo.orchestration_id);
        setSelectedStepId(null);
        replaceDraft({ ...orch }, { saved: true });
        setRunId(runInfo.run_id);
        // Sync the ref synchronously — the useEffect that mirrors runId only runs
        // after render, but streamRunJournal's guard reads currentRunIdRef immediately.
        currentRunIdRef.current = runInfo.run_id;
        setRunStatus(runInfo.status as 'running' | 'paused' | 'completed' | 'failed' | 'cancelled');
        setHumanPrompt(null);
        setHumanContext(null);

        // Baseline: mark all steps pending (like a fresh run) before overlaying
        // completed/failed from the checkpoint.
        const seeded: Record<string, RunStepStatus> = {};
        orch.steps.forEach(s => { seeded[s.id] = 'pending'; });
        setRunStepStatuses(seeded);

        // Restore step statuses + human-input state from the persisted checkpoint
        // so the canvas is meaningful before (and in case of a pre-journal run,
        // without) the journal replay.
        try {
            const res = await fetch(`/api/orchestrations/runs/${runInfo.run_id}`);
            if (res.ok) {
                const data = await res.json();
                if (Array.isArray(data.step_history)) {
                    setRunStepStatuses(prev => applyStepHistory(prev, data.step_history));
                }
                if (data.waiting_for_human && data.human_prompt) {
                    setHumanPrompt(data.human_prompt);
                    setHumanContext(data.human_context || null);
                    setHumanFields(fieldList(data.human_fields));
                    setHumanValues(initialHumanValues(fieldList(data.human_fields)));
                }
            }
        } catch { /* ignore */ }

        // Full-fidelity replay + live tail from the event journal. Falls back to
        // status polling for runs that predate the journal.
        const hasJournal = await streamRunJournal(runInfo.run_id, 0);
        if (!hasJournal) {
            setRunLog(['[This run predates event journaling — showing checkpoint status only.]']);
            if (runInfo.status === 'running') {
                reattachRun(runInfo.run_id, { silent: true });
            }
        }
        // streamRunJournal/reattachRun are stable useCallback([])s declared below;
        // adding them to the deps array would be a temporal-dead-zone read during render.
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [orchestrations]);

    // --- Select orchestration ---
    const selectOrchestration = useCallback((id: string | null) => {
        setSelectedOrchId(id);
        setSelectedStepId(null);
        setRunStatus('idle');
        setRunStepStatuses({});
        setRunLog([]);
        setHumanPrompt(null);
        if (id) {
            const orch = orchestrations.find(o => o.id === id);
            replaceDraft(orch ? { ...orch } : null, { saved: true });
        } else {
            replaceDraft(null);
        }
    }, [orchestrations, replaceDraft]);

    // --- Back to the landing dashboard ---
    // Detaches this browser from the run's event stream; the run itself keeps
    // executing server-side on the runner pump and stays listed under Active,
    // so leaving the view never interrupts work.
    const backToList = useCallback(() => {
        abortRef.current?.abort();
        abortRef.current = null;
        currentRunIdRef.current = null;  // stops streamRunJournal's tail loop
        setRunId(null);
        setLiveActivity(null);
        selectOrchestration(null);
    }, [selectOrchestration]);

    // --- Create new orchestration ---
    const createNew = () => {
        const id = 'orch_' + Math.random().toString(36).substring(2, 9);
        const orch: Orchestration = { ...EMPTY_ORCHESTRATION, id };
        replaceDraft(orch);
        setSelectedOrchId(id);
        setSelectedStepId(null);
    };

    // --- Duplicate orchestration ---
    const handleDuplicate = async () => {
        if (!draft) return;

        // Build old→new step ID map
        const idMap: Record<string, string> = {};
        for (const step of draft.steps) {
            idMap[step.id] = generateStepId();
        }

        // Remap a step ID reference, preserving null/undefined
        const remap = (id: string | null | undefined): string | null | undefined => {
            if (id == null) return id;
            return idMap[id] ?? id;
        };

        // Clone steps with remapped IDs
        const clonedSteps: StepConfig[] = draft.steps.map(step => ({
            ...step,
            id: idMap[step.id],
            next_step_id: remap(step.next_step_id) as string | undefined,
            route_map: step.route_map
                ? Object.fromEntries(
                    Object.entries(step.route_map).map(([label, target]) => [
                        label,
                        target != null ? (idMap[target as string] ?? target) : null,
                    ])
                  )
                : undefined,
            parallel_branches: step.parallel_branches?.map(branch =>
                branch.map(sid => idMap[sid] ?? sid)
            ),
            loop_step_ids: step.loop_step_ids?.map(sid => idMap[sid] ?? sid),
            // IF_ELSE remapping
            if_true_step_id: remap(step.if_true_step_id) as string | undefined,
            if_false_step_id: remap(step.if_false_step_id) as string | undefined,
            // SWITCH remapping
            switch_cases: step.switch_cases
                ? Object.fromEntries(
                    Object.entries(step.switch_cases).map(([val, target]) => [
                        val,
                        target != null ? (idMap[target as string] ?? target) : null,
                    ])
                  )
                : undefined,
            switch_default_step_id: remap(step.switch_default_step_id) as string | undefined,
        }));

        const newId = 'orch_' + Math.random().toString(36).substring(2, 9);
        const clone: Orchestration = {
            ...draft,
            id: newId,
            name: draft.name + ' (Copy)',
            steps: clonedSteps,
            entry_step_id: draft.entry_step_id ? (idMap[draft.entry_step_id] ?? '') : '',
            state_schema: JSON.parse(JSON.stringify(draft.state_schema ?? {})),
            created_at: undefined,
            updated_at: undefined,
        };

        setSaving(true);
        try {
            const res = await fetch('/api/orchestrations', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(clone),
            });
            if (res.ok) {
                const saved = await res.json();
                setOrchestrations(prev => [...prev, saved]);
                replaceDraft(saved, { saved: true });
                setSelectedOrchId(newId);
                setSelectedStepId(null);
            } else {
                showToast('Duplicate failed — the server rejected the copy', 'error');
            }
        } catch {
            showToast('Duplicate failed — could not reach the server', 'error');
        } finally {
            setSaving(false);
        }
    };

    // --- Save orchestration ---
    // Always saves — a half-built draft must be saveable — but names the
    // outcome either way. Configuration errors surface as node badges and a
    // toast here; they only *block* at Run.
    const handleSave = async () => {
        if (!draft) return;
        setSaving(true);
        try {
            const res = await fetch('/api/orchestrations', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(draft),
            });
            if (res.ok) {
                const saved = await res.json();
                const idx = orchestrations.findIndex(o => o.id === saved.id);
                if (idx >= 0) {
                    const next = [...orchestrations];
                    next[idx] = saved;
                    setOrchestrations(next);
                } else {
                    setOrchestrations([...orchestrations, saved]);
                }
                markSaved(saved);
                const { errorCount } = validateOrchestration(saved, agents);
                if (errorCount > 0) {
                    showToast(`Saved, with ${errorCount} configuration error${errorCount !== 1 ? 's' : ''} still to fix`, 'warning');
                } else {
                    showToast('Orchestration saved', 'success');
                }
            } else {
                const err = await res.json().catch(() => null);
                showToast(`Save failed: ${err?.detail || `HTTP ${res.status}`}`, 'error');
            }
        } catch {
            showToast('Save failed — could not reach the server', 'error');
        } finally {
            setSaving(false);
        }
    };

    // --- Delete orchestration ---
    const handleDelete = () => {
        if (!draft) return;
        setConfirmDeleteOrchId(draft.id);
    };

    const confirmDeleteOrchestration = async () => {
        if (!confirmDeleteOrchId) return;
        try {
            await fetch(`/api/orchestrations/${confirmDeleteOrchId}`, { method: 'DELETE' });
            setOrchestrations(orchestrations.filter(o => o.id !== confirmDeleteOrchId));
            if (draft?.id === confirmDeleteOrchId) {
                replaceDraft(null);
                setSelectedOrchId(null);
            }
        } catch {
            showToast('Delete failed — could not reach the server', 'error');
        }
    };

    // Steps are added from the palette inside WorkflowCanvas (drag-drop or
    // click), which owns placement and entry-point assignment via graph.ts.

    // --- Update step ---
    const updateStep = useCallback((updatedStep: StepConfig) => {
        if (!draft) return;
        setDraft({
            ...draft,
            steps: draft.steps.map(s => s.id === updatedStep.id ? updatedStep : s),
        });
    }, [draft, setDraft]);

    // --- Delete step --- (reference cleanup lives in the shared graph module)
    const deleteStep = useCallback((stepId: string) => {
        if (!draft) return;
        setDraft(removeStepFromGraph(draft, stepId));
        if (selectedStepId === stepId) setSelectedStepId(null);
    }, [draft, selectedStepId, setDraft]);

    // --- Duplicate the selected step (Cmd+D) ---
    const duplicateSelectedStep = useCallback(() => {
        if (!draft || !selectedStepId) return;
        const step = draft.steps.find(s => s.id === selectedStepId);
        if (!step) return;
        const copy = cloneStep(step);
        setDraft({ ...draft, steps: [...draft.steps, copy] });
        setSelectedStepId(copy.id);
    }, [draft, selectedStepId, setDraft]);

    // --- Set entry point ---
    const setEntryPoint = useCallback((stepId: string) => {
        if (!draft) return;
        setDraft({ ...draft, entry_step_id: stepId });
    }, [draft, setDraft]);

    // --- Update orchestration from canvas (position changes, edge connections) ---
    const updateOrchestration = useCallback((orch: Orchestration) => {
        setDraft(orch);
    }, [setDraft]);

    // --- Reattach stream: replay the run's event journal, then tail it live ---
    // The engine runs in a background task on the server and keeps executing
    // even if this client disconnects (e.g. the laptop slept), journaling every
    // event. Reattaching = GET the journal SSE stream: full replay from
    // `after`, then live tail until the run truly ends. Reconnects resume from
    // the last seen event id. Returns false iff the run has no journal
    // (predates the feature) so callers can fall back to status polling.
    const streamRunJournal = useCallback(async (rid: string, after: number): Promise<boolean> => {
        lastEventIdRef.current = after;
        let attempts = 0;
        while (true) {
            if (currentRunIdRef.current !== rid) return true;  // user switched runs
            const controller = new AbortController();
            abortRef.current?.abort();                          // never two feeds at once
            abortRef.current = controller;
            lastChunkAtRef.current = Date.now();
            try {
                const res = await fetch(
                    `/api/orchestrations/runs/${rid}/events/stream?after=${lastEventIdRef.current}`,
                    { signal: controller.signal },
                );
                if (res.status === 404) return false;           // pre-journal run
                if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`);
                if (lastEventIdRef.current === 0) {
                    // Full replay rebuilds the log from scratch — identical to
                    // having watched the run live, with no duplicated entries.
                    setRunLog([]);
                }
                attempts = 0;
                const reader = res.body.getReader();
                const decoder = new TextDecoder();
                let buffer = '';
                while (true) {
                    const { done, value } = await readWithStallTimeout(reader, controller);
                    if (done) break;
                    lastChunkAtRef.current = Date.now();
                    buffer += decoder.decode(value, { stream: true });
                    const lines = buffer.split('\n');
                    buffer = lines.pop() || '';
                    // Events in one chunk are processed synchronously — React
                    // batches the state updates, so a multi-hundred-event replay
                    // costs a handful of renders instead of one per event.
                    for (const line of lines) {
                        if (line.startsWith('id: ')) {
                            const id = parseInt(line.slice(4), 10);
                            if (!Number.isNaN(id)) lastEventIdRef.current = id;
                        } else if (line.startsWith('data: ')) {
                            try {
                                const data = JSON.parse(line.slice(6));
                                if (data.type === 'stream_complete') return true;  // run truly over
                                handleSSEEvent(data, true);
                            } catch { /* ignore parse errors */ }
                        }
                    }
                }
                // Stream ended without stream_complete (proxy drop, server
                // restart) — reconnect from where we left off.
                throw new Error('stream ended early');
            } catch (e: any) {
                // A terminal event handler or run switch aborts us on purpose;
                // 'recover' (wake handler) and transport errors mean reconnect.
                const intentional = e?.name === 'AbortError' && controller.signal.reason !== 'recover';
                if (intentional) return true;
                if (++attempts > 20) {
                    setRunLog(prev => [...prev, '[Reconnect failed — see Active Runs to rejoin]']);
                    return true;
                }
                await new Promise<void>(r => setTimeout(r, Math.min(3000, 500 * attempts)));
            } finally {
                if (abortRef.current === controller) abortRef.current = null;
            }
        }
    }, []);

    // --- Re-attach to a run after the SSE connection drops ---
    // Journal replay is the primary path; polling remains only for runs that
    // predate event journaling.
    const reattachRun = useCallback(async (rid: string | null, opts?: { silent?: boolean }) => {
        if (!rid) { setRunStatus('failed'); setRunLog(prev => [...prev, '[Connection lost]']); return; }
        if (reattachActiveRef.current) return;       // already reconnecting
        reattachActiveRef.current = true;
        try {
            const hasJournal = await streamRunJournal(rid, 0);
            if (!hasJournal) await pollRunStatus(rid, opts);
        } finally {
            reattachActiveRef.current = false;
        }
        // streamRunJournal/pollRunStatus are stable useCallback([])s.
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);

    // Legacy fallback: track a pre-journal run to completion by polling its
    // step-boundary checkpoint.
    const pollRunStatus = useCallback(async (rid: string, opts?: { silent?: boolean }) => {
        if (!opts?.silent) setRunLog(prev => [...prev, '[Connection lost — reconnecting to run…]']);
        const sleep = (ms: number) => new Promise<void>(r => setTimeout(r, ms));
        const deadline = Date.now() + 10 * 60 * 1000; // give up after 10 min
        let notFound = 0; // tolerate brief 404 windows at run start/end before giving up
        {
            while (Date.now() < deadline) {
                if (currentRunIdRef.current !== rid) return;  // user started/restored a different run
                let data: any;
                try {
                    const res = await fetch(`/api/orchestrations/runs/${rid}`);
                    if (res.status === 404) {
                        if (++notFound >= 3) {
                            setRunStatus('failed');
                            setRunLog(prev => [...prev, '[Reconnect failed — run not found]']);
                            return;
                        }
                        await sleep(3000); continue;
                    }
                    if (!res.ok) { await sleep(3000); continue; }
                    notFound = 0;
                    data = await res.json();
                } catch { await sleep(3000); continue; }

                // Sync step statuses from the persisted history
                if (Array.isArray(data.step_history)) {
                    setRunStepStatuses(prev => applyStepHistory(prev, data.step_history));
                }

                if (data.status === 'running') {
                    setRunStatus('running');
                    await sleep(3000);
                    continue;
                }
                if (data.status === 'paused') {
                    setRunStatus('paused');
                    if (data.waiting_for_human && data.human_prompt) {
                        setHumanPrompt(data.human_prompt);
                        setHumanContext(data.human_context || null);
                        setHumanFields(fieldList(data.human_fields));
                        setHumanValues(initialHumanValues(fieldList(data.human_fields)));
                    }
                    setRunLog(prev => [...prev, '[Reconnected — run is waiting for your input]']);
                    return;
                }
                // Terminal: completed / failed / cancelled
                const final = data.status === 'completed' ? 'completed' : data.status === 'cancelled' ? 'cancelled' : 'failed';
                setRunStatus(final);
                setHumanPrompt(null);
                setHumanContext(null);
                setRunLog(prev => [...prev, `[Reconnected — run ${data.status}]`]);
                return;
            }
            setRunLog(prev => [...prev, '[Reconnect timed out — see Active Runs to rejoin]']);
        }
    }, []);

    // --- SSE stream reader helper (POST run/resume/human-input streams) ---
    const streamSSE = async (
        url: string,
        body: Record<string, any>,
        /** A caller that can say why its request was refused, and what now. */
        onRefused?: (res: Response) => Promise<void>,
    ) => {
        const controller = new AbortController();
        abortRef.current?.abort();  // e.g. close a reattach tail before resuming
        abortRef.current = controller;
        lastChunkAtRef.current = Date.now();

        try {
            const res = await fetch(url, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
                signal: controller.signal,
            });
            if (!res.ok && onRefused) {
                await onRefused(res);
                return;
            }
            if (!res.ok || !res.body) {
                setRunStatus('failed');
                setRunLog(prev => [...prev, `[HTTP ${res.status}]`]);
                return;
            }

            const reader = res.body.getReader();
            const decoder = new TextDecoder();
            let buffer = '';

            while (true) {
                const { done, value } = await readWithStallTimeout(reader, controller);
                if (done) break;
                lastChunkAtRef.current = Date.now();  // heartbeats keep this fresh while alive
                buffer += decoder.decode(value, { stream: true });
                const lines = buffer.split('\n');
                buffer = lines.pop() || '';
                for (const line of lines) {
                    if (line.startsWith('data: ')) {
                        try {
                            const data = JSON.parse(line.slice(6));
                            handleSSEEvent(data);
                            // Yield to the macrotask queue so React flushes
                            // state updates between events rather than batching
                            // them all into a single render at stream end.
                            await new Promise<void>(resolve => setTimeout(resolve, 0));
                        } catch { /* ignore parse errors */ }
                    }
                }
            }
        } catch (e: any) {
            // A stall/transport error, or a 'recover' abort from the wake handler,
            // means the connection dropped while the run is likely still executing
            // server-side → re-attach via polling. A plain AbortError is an
            // intentional stop (cancel / unmount / terminal event) → ignore.
            const lostConnection = e.name !== 'AbortError' || controller.signal.reason === 'recover';
            if (lostConnection) {
                reattachRun(currentRunIdRef.current);
            }
        } finally {
            abortRef.current = null;
        }
    };

    // --- Run orchestration ---
    const startRun = () => {
        if (!draft) return;
        // Errors are exactly the class the engine would fail on mid-run —
        // dangling targets, an agent step with no agent. Block here, where the
        // author can still fix them, instead of three steps into a paid run.
        const { errorCount } = validateOrchestration(draft, agents);
        if (errorCount > 0) {
            showToast(`Fix ${errorCount} configuration error${errorCount !== 1 ? 's' : ''} before running — see the marked steps`, 'error');
            return;
        }
        if (dirty) {
            showToast('You have unsaved changes — the run uses the last saved version', 'warning');
        }
        const statuses: Record<string, 'pending' | 'running' | 'completed' | 'failed'> = {};
        draft.steps.forEach(s => { statuses[s.id] = 'pending'; });
        setRunStepStatuses(statuses);
        setRunStatus('running');
        setRunLog([]);
        setHumanPrompt(null);
        setLiveActivity(null);
        lastEventIdRef.current = 0;  // fresh run, fresh journal

        streamSSE(`/api/orchestrations/${draft.id}/run`, { message: runInput });
    };

    // `fromJournal` marks events arriving from the replay/tail stream. A
    // journal can contain a terminal event mid-file (cancel, then resume
    // appends more events to the same run), so terminal handlers must NOT
    // abort the feed there — the server signals the real end with
    // `stream_complete`. Aborting on the live POST stream stays correct.
    const handleSSEEvent = (data: any, fromJournal = false) => {
        switch (data.type) {
            case 'orchestration_start':
                setRunId(data.run_id);
                setRunLog(prev => [...prev, line('muted', `Started run ${data.run_id}`)]);
                break;

            case 'step_start': {
                // Progress after a pause means the pending input was consumed
                // (submitted here, in another tab, or via messaging) — drop the
                // stale form. Matters for journal replay of resumed runs too.
                setHumanPrompt(null);
                setHumanContext(null);
                setRunStatus('running');
                setRunStepStatuses(prev => ({ ...prev, [data.orch_step_id]: 'running' }));
                setRunLog(prev => [...prev, line('start', `${data.step_name} (${data.step_type})`)]);
                // Begin tracking response for agent/llm/print/extract_json steps, keyed by step id
                if (data.step_type === 'agent' || data.step_type === 'llm' || data.step_type === 'print' || data.step_type === 'extract_json') {
                    pendingStepResultRef.current.set(data.orch_step_id, {
                        step_name: data.step_name,
                        step_type: data.step_type as 'agent' | 'llm' | 'print' | 'extract_json',
                        content: '',
                    });
                } else {
                    pendingStepResultRef.current.delete(data.orch_step_id);
                }
                break;
            }

            case 'step_complete': {
                setLiveActivity(null);
                setRunStepStatuses(prev => ({ ...prev, [data.orch_step_id]: 'completed' }));
                const pendingForStep = pendingStepResultRef.current.get(data.orch_step_id);
                setRunLog(prev => {
                    const next = [...prev, line('ok', `${data.step_name} completed (${data.duration_seconds?.toFixed(1)}s)`)];
                    if (pendingForStep && pendingForStep.content.trim()) {
                        next.splice(next.length - 1, 0, {
                            kind: 'step_result',
                            step_name: pendingForStep.step_name,
                            step_type: pendingForStep.step_type,
                            content: pendingForStep.content.trim(),
                        } as StepResultLogEntry);
                    }
                    return next;
                });
                pendingStepResultRef.current.delete(data.orch_step_id);
                break;
            }

            case 'step_error':
                setLiveActivity(null);
                setRunStepStatuses(prev => ({ ...prev, [data.orch_step_id]: 'failed' }));
                setRunLog(prev => [...prev, line('error', `Step error: ${data.error}`)]);
                pendingStepResultRef.current.delete(data.orch_step_id);
                break;

            case 'llm_reasoning':
                if (data.reasoning) {
                    setLiveActivity(`reasoning · ${String(data.reasoning).split('\n')[0].slice(0, 140)}`);
                    setRunLog(prev => [...prev, {
                        kind: 'reasoning',
                        step_name: data.step_name,
                        content: String(data.reasoning),
                    } as ReasoningLogEntry]);
                }
                break;

            case 'llm_thought':
                if (data.thought) {
                    setRunLog(prev => [...prev, {
                        kind: 'thought',
                        step_name: data.step_name,
                        content: String(data.thought),
                    } as ThoughtLogEntry]);
                }
                break;

            case 'thinking':
                // Chatty progress line ("Analyzing...", "Delegating to X...") —
                // shown as the live activity indicator, not appended to the log.
                if (data.message) setLiveActivity(`thinking · ${data.message}`);
                break;

            case 'status':
                if (data.message) setRunLog(prev => [...prev, line('info', data.message)]);
                break;

            case 'step_warning':
                setRunLog(prev => [...prev, line('warn', data.message || 'Step warning')]);
                break;

            case 'context_compact':
                setRunLog(prev => [...prev,
                    line('compact', `Context compacted (${data.chars_before ?? '?'} → ${data.chars_after ?? '?'} chars)`)]);
                break;

            case 'final': {
                // Capture the full final response keyed by step id
                const response = data.response || '';
                const stepId = data.orch_step_id || '';
                if (stepId && pendingStepResultRef.current.has(stepId) && response) {
                    pendingStepResultRef.current.get(stepId)!.content = response;
                }
                break;
            }

            case 'evaluator_decision': {
                // The evaluator step's own event: the route the model picked.
                // It answered to `routing_decision` before, which carries
                // neither of these fields — so this line read
                // `Evaluator routed → undefined` on every evaluator run.
                const picked = String(data.tool_name || '').replace(/^route_/, '');
                const why = (data.arguments as Record<string, unknown> | undefined)?.reasoning;
                setRunLog(prev => [...prev, line('flow',
                    `Evaluator chose ${picked || '(no route)'}${why ? ` (${why})` : ''}`)]);
                break;
            }

            case 'routing_decision': {
                // One router event, three routing step types; `step_type` says
                // which. Absent means an archived run from before the rename,
                // when only the evaluator used this name.
                const st = data.step_type as string | undefined;
                const routed = st === 'if_else'
                    ? `Branch: ${data.decision ?? 'false'} → ${data.target_step_id || 'end'}`
                    : st === 'switch'
                        ? `Branch: case ${data.decision ?? 'default'} → ${data.target_step_id || 'end'}`
                        : `Evaluator routed → ${data.decision}${data.reasoning ? ` (${data.reasoning})` : ''}`;
                setRunLog(prev => [...prev, line('flow', routed)]);
                break;
            }

            // The step's own events. The router used to share these names with
            // a payload that had no field in common; it emits `routing_decision`
            // now. The second arm stays for runs archived before the rename.
            case 'if_decision':
                setRunLog(prev => [...prev, line('flow', data.condition !== undefined
                    ? `If/Else: ${data.condition} → ${data.result}`
                    : `Branch: ${data.decision ?? 'false'} → ${data.target_step_id || 'end'}`)]);
                break;

            case 'switch_decision':
                setRunLog(prev => [...prev, line('flow', data.expression !== undefined
                    ? `Switch: ${data.expression} = "${data.value}" → ${data.matched_case ?? 'default'}`
                    : `Branch: case ${data.matched_case ?? 'default'} → ${data.target_step_id || 'end'}`)]);
                break;

            case 'parallel_start':
                setRunLog(prev => [...prev, line('parallel', `Parallel: running ${data.branch_count} branches`)]);
                break;

            case 'branch_start':
                setRunLog(prev => [...prev, line('branch', `Branch ${(data.branch_index ?? 0) + 1}/${data.branch_count}`)]);
                break;

            case 'parallel_complete':
                setRunLog(prev => [...prev, line('parallel', `Parallel: all ${data.branch_count} branches done`)]);
                break;

            case 'loop_iteration':
                setRunLog(prev => [...prev, line('loop', `Loop iteration ${data.iteration}/${data.total}`)]);
                break;

            case 'merge_complete':
                setRunLog(prev => [...prev, line('merge', `Merged ${data.input_count} inputs (${data.strategy})`)]);
                break;

            case 'orchestration_end':
                setRunLog(prev => [...prev, line('end', 'End node reached')]);
                break;

            case 'human_input_required':
                setLiveActivity(null);
                setRunStatus('paused');
                if (data.orch_step_id) setRunStepStatuses(prev => ({ ...prev, [data.orch_step_id]: 'paused' }));
                setHumanPrompt(data.prompt || 'Please provide input:');
                setHumanContext(data.agent_context || null);
                setHumanFields(fieldList(data.fields));
                setHumanValues(initialHumanValues(fieldList(data.fields)));
                setRunLog(prev => [...prev, line('pause', 'Waiting for human input…')]);
                break;

            case 'loop_limit_reached':
                setRunLog(prev => [...prev, line('loop', `Loop limit reached for step ${data.orch_step_id} (${data.iterations} iterations)`)]);
                break;

            case 'orchestration_complete':
                setLiveActivity(null);
                setHumanPrompt(null);
                setHumanContext(null);
                setRunStatus(data.status === 'completed' ? 'completed' : 'failed');
                setRunLog(prev => [...prev, line(data.status === 'completed' ? 'ok' : 'error', `Done — status: ${data.status}`)]);
                if (!fromJournal) {
                    abortRef.current?.abort();
                    abortRef.current = null;
                }
                break;

            case 'orchestration_error':
                setLiveActivity(null);
                setHumanPrompt(null);
                setHumanContext(null);
                setRunStatus('failed');
                setRunLog(prev => [...prev, line('error', `Error: ${data.error}`)]);
                if (!fromJournal) {
                    abortRef.current?.abort();
                    abortRef.current = null;
                }
                break;

            case 'tool_execution':
                // update_tasks renders as the live checklist (tasks_updated) —
                // the raw call/result pair would only be noise around it.
                if (data.tool_name === 'update_tasks') {
                    setLiveActivity('tasks · updating plan');
                    break;
                }
                setLiveActivity(`tool · ${data.tool_name}`);
                setRunLog(prev => [...prev, {
                    kind: 'tool_call',
                    tool_name: data.tool_name,
                    args: data.args || {},
                    step_name: data.step_name,
                } as ToolCallLogEntry]);
                break;

            case 'tool_result':
                if (data.tool_name === 'update_tasks') break;
                setRunLog(prev => [...prev, {
                    kind: 'tool_result',
                    tool_name: data.tool_name,
                    preview: data.preview || '',
                } as ToolResultLogEntry]);
                break;

            case 'tasks_updated': {
                // One live checklist: the task list is run-global state, so the
                // previous snapshot is superseded — re-render it at the tail,
                // where the action is (journal replay reproduces the same walk).
                const tasks = Array.isArray(data.tasks) ? data.tasks : [];
                setRunLog(prev => [
                    ...prev.filter(e => typeof e === 'string' || e.kind !== 'tasks'),
                    { kind: 'tasks', step_name: data.step_name, tasks } as TasksLogEntry,
                ]);
                break;
            }

            case 'tasks_incomplete': {
                const n = Array.isArray(data.tasks) ? data.tasks.length : 0;
                setRunLog(prev => [...prev, line('warn', `Step ended with ${n} open task${n !== 1 ? 's' : ''}`)]);
                break;
            }

            case 'token_usage':
                // Silently track
                break;

            default:
                // chunk events are accumulated in pendingStepResultRef (via 'final'),
                // so we only show a live streaming indicator if there's no pending capture
                if (data.type === 'chunk' && data.content && !pendingStepResultRef.current) {
                    setRunLog(prev => {
                        const last = prev[prev.length - 1];
                        if (last && typeof last === 'string' && last.startsWith('  ')) {
                            return [...prev.slice(0, -1), last + data.content];
                        }
                        return [...prev, '  ' + data.content];
                    });
                }
                break;
        }
    };

    const cancelRun = async () => {
        setLiveActivity(null);
        abortRef.current?.abort();
        abortRef.current = null;
        if (runId) {
            try {
                await fetch(`/api/orchestrations/runs/${runId}/cancel`, { method: 'POST' });
            } catch { /* ignore */ }
        }
        setRunStatus('cancelled');
        setRunLog(prev => [...prev, '[Cancelled]']);
    };

    const submitHumanInput = async () => {
        if (!runId) return;
        setHumanPrompt(null);
        setHumanContext(null);
        setRunStatus('running');
        setRunStepStatuses(prev => {
            const next = { ...prev };
            for (const k in next) { if (next[k] === 'paused') next[k] = 'running'; }
            return next;
        });
        setRunLog(prev => [...prev, `Human response submitted`]);
        const response = humanFields.length ? humanAnswer(humanFields, humanValues) : humanResponse;
        setHumanResponse('');

        streamSSE(`/api/orchestrations/runs/${runId}/human-input`, { response }, async (res) => {
            // Refused (F215): most often someone else answered first. Say so,
            // then follow the run on from the last event this tab saw, which
            // keeps this line in the log; a replay from 0 would rebuild it.
            const message = await refusedAnswerMessage(res);
            showToast(message, 'warning');
            setRunLog(prev => [...prev, `[${message}]`]);
            if (res.status === 404) { setRunStatus('failed'); return; }
            const hasJournal = await streamRunJournal(runId, lastEventIdRef.current);
            if (!hasJournal) await pollRunStatus(runId, { silent: true });
        });
    };

    const resumeRun = async () => {
        if (!runId) return;
        setRunStatus('running');
        setRunLog(prev => [...prev, '[Resuming from where run stopped...]']);
        streamSSE(`/api/orchestrations/runs/${runId}/resume`, {});
    };

    // Keep a ref mirror of runId so async handlers read the live value.
    useEffect(() => { currentRunIdRef.current = runId; }, [runId]);

    // Deep link (?id=<orchestration_id>): open it on the canvas, exactly as if
    // its row had been clicked. `HowImBuilt` has been sending people here since
    // it shipped — "duplicate the builder" ends with
    // `window.location.href = /orchestrations?id=…` — and the page read only
    // `?run=`, so the link landed on the list with nothing selected.
    const initialOrchConsumedRef = useRef(false);
    useEffect(() => {
        if (!initialOrchId || initialOrchConsumedRef.current || orchestrations.length === 0) return;
        if (!orchestrations.some((o) => o.id === initialOrchId)) return;
        initialOrchConsumedRef.current = true;
        selectOrchestration(initialOrchId);
    }, [initialOrchId, orchestrations, selectOrchestration]);

    // Deep link (?run=<run_id>): once orchestrations are loaded, open that run
    // exactly as if it was clicked in the Active Runs banner.
    const initialRunConsumedRef = useRef(false);
    useEffect(() => {
        if (!initialRunId || initialRunConsumedRef.current || orchestrations.length === 0) return;
        initialRunConsumedRef.current = true;
        (async () => {
            try {
                const res = await fetch(`/api/orchestrations/runs/${initialRunId}`);
                if (!res.ok) return;
                const data = await res.json();
                restoreRun({
                    run_id: initialRunId,
                    orchestration_id: data.orchestration_id || '',
                    status: data.status || 'running',
                });
            } catch { /* ignore */ }
        })();
        // restoreRun is recreated when orchestrations load; the consumed ref
        // keeps this one-shot.
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [initialRunId, orchestrations]);

    // Wake / network recovery: on refocus or network-back, if a run's stream has
    // gone silent past several heartbeat windows (≈ dead socket after sleep),
    // abort it with a 'recover' reason so streamSSE re-attaches to the run.
    // A recent chunk (heartbeat <60s ago) means it's alive — leave it be.
    useEffect(() => {
        const maybeRecover = () => {
            if (!abortRef.current) return;                         // no active stream
            if (document.visibilityState === 'hidden') return;
            if (Date.now() - lastChunkAtRef.current <= 60000) return;
            abortRef.current.abort('recover');
        };
        window.addEventListener('online', maybeRecover);
        document.addEventListener('visibilitychange', maybeRecover);
        return () => {
            window.removeEventListener('online', maybeRecover);
            document.removeEventListener('visibilitychange', maybeRecover);
        };
    }, []);

    // Cleanup on unmount
    useEffect(() => {
        return () => {
            abortRef.current?.abort();
        };
    }, []);

    const selectedStep = draft?.steps.find(s => s.id === selectedStepId) || null;
    const allStepIds = draft?.steps.map(s => ({ id: s.id, name: s.name })) || [];

    // Issues drive the toolbar chip and the Run gate; the canvas badges nodes
    // and the panel explains — all from the same validate.ts pass.
    const validation = draft ? validateOrchestration(draft, agents) : null;

    // The builder's "this one" is whatever this screen is showing, for exactly
    // as long as it is showing it — including across a client-side navigation,
    // which leaves the panel open and used to leave the old answer with it.
    const realOrchId = selectedOrchId && orchestrations.some(o => o.id === selectedOrchId)
        ? selectedOrchId
        : null;
    useEffect(() => {
        setCurrentOrchestration(realOrchId);
        return () => setCurrentOrchestration(null);
    }, [realOrchId, setCurrentOrchestration]);

    useBuilderShortcuts({
        enabled: !!draft,
        undo,
        redo,
        onDuplicate: duplicateSelectedStep,
        onSave: handleSave,
    });

    // --- Deploy as agent ---
    const handleDeploy = async () => {
        if (!draft) return;
        try {
            const res = await fetch(`/api/orchestrations/${draft.id}/deploy`, { method: 'POST' });
            if (res.ok) {
                const data = await res.json();
                showToast(`Deployed as agent "${draft.name}" (${data.agent_id})`, 'success');
            } else {
                const err = await res.json();
                showToast(`Deploy failed: ${err.detail || 'Unknown error'}`, 'error');
            }
        } catch {
            showToast('Failed to deploy orchestration as agent', 'error');
        }
    };

    const visibleOrchestrations = orchestrations.filter(
        o => matchesQuery(orchQuery, o.name, o.description));

    return (
        <div className="flex flex-col h-full relative">
            {toast && <ToastNotification show={toast.show} message={toast.message} type={toast.type} />}
            {/* Toolbar: orchestration picker + actions */}
            <div className="flex items-center gap-3 px-4 py-2.5 border-b border-zinc-800 shrink-0">
                <div className="flex items-center gap-2 flex-1 min-w-0">
                    {draft && (
                        <button
                            onClick={backToList}
                            title="Back to all orchestrations and runs"
                            className="flex items-center gap-1 px-2.5 py-1.5 text-xs text-zinc-400 hover:text-white border border-zinc-700 hover:border-white transition-colors shrink-0"
                        >
                            <ArrowLeft size={14} /> Back
                        </button>
                    )}
                    <Combobox
                        value={selectedOrchId || undefined}
                        onChange={(id: string) => selectOrchestration(id || null)}
                        placeholder="Select orchestration…"
                        searchPlaceholder="Search orchestrations…"
                        aria-label="Orchestration"
                        size="sm"
                        className="max-w-[240px]"
                        options={orchestrations.map(o => ({ value: o.id, label: o.name }))}
                    />
                    <button
                        onClick={createNew}
                        className="flex items-center gap-1 px-3 py-1.5 text-xs bg-blue-600 hover:bg-blue-500 text-white transition-colors"
                    >
                        <Plus size={14} /> New
                    </button>
                    <button
                        onClick={() => openBuilder()}
                        className="flex items-center gap-1.5 px-3 py-1.5 text-xs bg-accent hover:bg-accent-hover text-accent-fg transition-colors"
                    >
                        <Mark size={14} /> Build with AI
                    </button>
                </div>

                {draft && (
                    <div className="flex items-center gap-1.5 pr-6">
                        {validation && validation.errorCount + validation.warningCount > 0 && (
                            <Hint
                                content={[
                                    ...validation.global.map(i => i.message),
                                    ...Object.values(validation.byStep).flat().map(i => i.message),
                                ].slice(0, 6).join(' · ')}
                            >
                                <span className={`inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs ${validation.errorCount > 0 ? 'bg-danger-subtle text-danger' : 'bg-warning-subtle text-warning'}`}>
                                    <AlertTriangle size={12} aria-hidden />
                                    {validation.errorCount > 0
                                        ? `${validation.errorCount} error${validation.errorCount !== 1 ? 's' : ''}`
                                        : `${validation.warningCount} warning${validation.warningCount !== 1 ? 's' : ''}`}
                                </span>
                            </Hint>
                        )}
                        <IconButton label="Undo (Ctrl+Z)" icon={Undo2} size="sm" onClick={undo} disabled={!canUndo} />
                        <IconButton label="Redo (Ctrl+Shift+Z)" icon={Redo2} size="sm" onClick={redo} disabled={!canRedo} />
                        <div className="mx-1 h-5 w-px bg-border-strong" />
                        <Button size="sm" variant="secondary" onClick={handleSave} disabled={saving} title={dirty ? 'Unsaved changes (Ctrl+S)' : 'Saved (Ctrl+S)'}>
                            {saving ? <Spinner size={16} /> : <Save size={14} aria-hidden />}
                            Save
                            {dirty && <span className="size-1.5 rounded-full bg-accent" aria-label="Unsaved changes" />}
                        </Button>
                        {runStatus === 'idle' || runStatus === 'completed' || runStatus === 'failed' || runStatus === 'cancelled' ? (
                            <Button size="sm" variant="primary" onClick={startRun}>
                                <Play size={14} aria-hidden /> Run
                            </Button>
                        ) : (
                            <Button size="sm" variant="danger" onClick={cancelRun}>
                                <Square size={14} aria-hidden /> Cancel
                            </Button>
                        )}
                        <Button size="sm" variant="secondary" onClick={handleDeploy}>
                            Deploy as Agent
                        </Button>
                        <Button size="sm" variant="secondary" onClick={handleDuplicate} disabled={saving}>
                            <Copy size={13} aria-hidden /> Duplicate
                        </Button>
                        <div className="mx-1 h-5 w-px bg-border-strong" />
                        <Button size="sm" variant="ghost" className="text-text-faint hover:text-danger" onClick={handleDelete}>
                            <Trash size={13} aria-hidden /> Delete
                        </Button>
                    </div>
                )}
            </div>

            {/* Active runs banner */}
            {activeRuns.filter(r => r.run_id !== runId).length > 0 && (
                <div className="px-4 py-2 border-b border-zinc-800 bg-zinc-900/60 shrink-0">
                    <div className="flex items-center gap-2 flex-wrap">
                        <span className="text-xs text-text-faint flex items-center gap-1">
                            <Radio size={11} className="text-blue-400 animate-pulse" /> Active runs:
                        </span>
                        {activeRuns.filter(r => r.run_id !== runId).map(run => {
                            const orch = orchestrations.find(o => o.id === run.orchestration_id);
                            return (
                                <button
                                    key={run.run_id}
                                    onClick={() => restoreRun(run)}
                                    className="flex items-center gap-1.5 px-2.5 py-1 text-xs bg-zinc-800 hover:bg-zinc-700 border border-zinc-700 transition-colors rounded-md"
                                >
                                    <span className={`w-1.5 h-1.5 rounded-full ${
                                        run.status === 'running' ? 'bg-blue-400 animate-pulse' : 'bg-yellow-400'
                                    }`} />
                                    <span className="text-zinc-300">{orch?.name ?? run.orchestration_id}</span>
                                    <span className="text-text-faint">{run.status === 'paused' ? '· waiting for input' : '· running'}</span>
                                </button>
                            );
                        })}
                    </div>
                </div>
            )}

            {!draft ? (
                /* ── Landing dashboard: tabbed tables ────────────────────── */
                <div className="flex-1 overflow-y-auto modern-scrollbar">
                    <div className="px-4 pb-8">
                        {/* Tab bar */}
                        <div className="flex items-end border-b border-border mb-4">
                            <div className="flex items-center">
                                {([
                                    { id: 'orchestrations', label: 'Orchestrations', count: orchestrations.length },
                                    { id: 'active', label: 'Active', count: activeRuns.length },
                                    { id: 'recent', label: 'Recent', count: null },
                                    { id: 'all', label: 'All runs', count: null },
                                ] as const).map(t => (
                                    <button
                                        key={t.id}
                                        onClick={() => setLandingTab(t.id)}
                                        className={`px-4 py-2.5 text-xs font-medium border-b -mb-px transition-colors ${
                                            landingTab === t.id
                                                ? 'text-zinc-100 border-zinc-100'
                                                : 'text-text-faint border-transparent hover:text-zinc-300'
                                        }`}
                                    >
                                        {t.label}
                                        {t.count !== null && t.count > 0 && (
                                            <span className={`ml-1.5 px-1.5 py-0.5 text-[10px] ${
                                                t.id === 'active'
                                                    ? 'bg-blue-500/20 text-blue-300'
                                                    : 'bg-surface-2 text-zinc-400'
                                            }`}>{t.count}</span>
                                        )}
                                    </button>
                                ))}
                            </div>
                        </div>

                        {landingTab === 'orchestrations' && orchestrations.length > 5 && (
                            <SearchInput
                                value={orchQuery}
                                onChange={setOrchQuery}
                                placeholder="Search orchestrations by name or description…"
                                className="mb-3 max-w-md"
                            />
                        )}

                        {landingTab === 'orchestrations' && (
                            <div className="border border-border bg-zinc-900/60 rounded-md">
                                <div className="overflow-x-auto">
                                    <table className="w-full text-left border-collapse">
                                        <thead>
                                            <tr className="border-b border-border bg-zinc-950/40">
                                                <th className={thCls}>Name</th>
                                                <th className={thCls}>Description</th>
                                                <th className={`${thCls} text-right`}>Steps</th>
                                                <th className={`${thCls} text-right`}>Last run</th>
                                                <th className={thCls}></th>
                                            </tr>
                                        </thead>
                                        <tbody>
                                            {visibleOrchestrations.length === 0 ? (
                                                <tr>
                                                    <td colSpan={5} className="px-4 py-10 text-center text-xs text-text-faint italic">
                                                        {orchQuery
                                                            ? `No orchestrations match “${orchQuery}”.`
                                                            : 'No orchestrations yet — create one or build with AI.'}
                                                    </td>
                                                </tr>
                                            ) : visibleOrchestrations.map(o => {
                                                const lastRun = pastRuns.find(r => r.orchestration_id === o.id);
                                                const lastMeta = lastRun ? runStatusMeta(lastRun) : null;
                                                return (
                                                    <tr
                                                        key={o.id}
                                                        onClick={() => selectOrchestration(o.id)}
                                                        className="border-b border-border last:border-b-0 hover:bg-row-hover cursor-pointer transition-colors group"
                                                    >
                                                        <td className={`${tdCls} text-zinc-200 font-medium whitespace-nowrap max-w-[240px]`}>
                                                            <span className="block truncate">{o.name}</span>
                                                        </td>
                                                        <td className={`${tdCls} text-text-faint max-w-[380px]`}>
                                                            <span className="block truncate">{o.description || '—'}</span>
                                                        </td>
                                                        <td className={`${tdCls} text-right text-zinc-400 whitespace-nowrap`}>
                                                            {o.steps.length}
                                                        </td>
                                                        <td className={`${tdCls} text-right whitespace-nowrap`}>
                                                            {lastRun && lastMeta ? (
                                                                <span className="inline-flex items-center gap-1.5 justify-end">
                                                                    <span className={`w-1.5 h-1.5 rounded-full ${lastMeta.dot}`} />
                                                                    <span className="text-text-faint">
                                                                        {lastRun.started_at ? new Date(lastRun.started_at).toLocaleString() : lastMeta.label}
                                                                    </span>
                                                                </span>
                                                            ) : <span className="text-text-faint">never</span>}
                                                        </td>
                                                        <td className={`${tdCls} text-right whitespace-nowrap`}>
                                                            <span className="text-[11px] text-text-faint group-hover:text-zinc-300 transition-colors">
                                                                Open →
                                                            </span>
                                                        </td>
                                                    </tr>
                                                );
                                            })}
                                        </tbody>
                                    </table>
                                </div>
                            </div>
                        )}

                        {landingTab === 'active' && renderRunsTable(
                            activeRuns,
                            'No active runs — start one from the Orchestrations tab.',
                        )}

                        {landingTab === 'recent' && renderRunsTable(
                            pastRuns.filter(r => r.status !== 'running' && r.status !== 'paused').slice(0, 10),
                            'No finished runs yet.',
                        )}

                        {landingTab === 'all' && renderRunsTable(
                            allRuns.length > 0 ? allRuns : pastRuns,
                            'No runs recorded yet.',
                        )}
                    </div>
                </div>
            ) : (
                <div className="flex-1 flex flex-col min-h-0">
                    {/* Name + description. The step palette lives inside the canvas. */}
                    <div className="flex items-center gap-3 px-4 py-2 border-b border-zinc-800 shrink-0">
                        <input
                            aria-label="Orchestration name"
                            className="bg-transparent border-b border-zinc-700 text-zinc-200 text-sm font-medium px-1 py-0.5 outline-none focus:border-accent w-64"
                            value={draft.name}
                            onChange={(e) => setDraft({ ...draft, name: e.target.value })}
                            placeholder="Orchestration name"
                        />
                        <input
                            aria-label="Orchestration description"
                            className="bg-transparent border-b border-zinc-700 text-zinc-400 text-xs px-1 py-0.5 outline-none focus:border-accent flex-1 mr-6"
                            value={draft.description}
                            onChange={(e) => setDraft({ ...draft, description: e.target.value })}
                            placeholder="Description..."
                        />
                    </div>

                    {/* Main content: canvas + optional side panel */}
                    <div className="flex-1 flex min-h-0">
                        {/* Canvas */}
                        <div className="flex-1 min-w-0">
                            <ReactFlowProvider>
                                <WorkflowCanvas
                                    orchestration={draft}
                                    agents={agents}
                                    selectedStepId={selectedStepId}
                                    onSelectStep={setSelectedStepId}
                                    onUpdateOrchestration={updateOrchestration}
                                    runStepStatuses={runStepStatuses}
                                />
                            </ReactFlowProvider>
                        </div>

                        {/* Step config panel */}
                        {selectedStep && (
                            <StepConfigPanel
                                step={selectedStep}
                                agents={agents}
                                allStepIds={allStepIds}
                                onUpdate={updateStep}
                                onDelete={() => deleteStep(selectedStep.id)}
                                onClose={() => setSelectedStepId(null)}
                                isEntry={draft.entry_step_id === selectedStep.id}
                                onSetEntry={() => setEntryPoint(selectedStep.id)}
                                availableModels={availableModels}
                                orchestration={draft}
                            />
                        )}
                    </div>

                    {/* Bottom panel: state schema + guardrails + run log */}
                    <div className="border-t border-zinc-700 bg-zinc-900 shrink-0">
                        <BottomPanel
                            draft={draft}
                            setDraft={setDraft}
                            runStatus={runStatus}
                            liveActivity={liveActivity}
                            runLog={runLog}
                            runInput={runInput}
                            setRunInput={setRunInput}
                            onStartRun={startRun}
                            humanPrompt={humanPrompt}
                            humanContext={humanContext}
                            humanResponse={humanResponse}
                            setHumanResponse={setHumanResponse}
                            humanFields={humanFields}
                            humanValues={humanValues}
                            setHumanValues={setHumanValues}
                            onSubmitHuman={submitHumanInput}
                            onOpenResponseModal={setResponseModal}
                            runId={runId}
                            rubrics={rubrics}
                            onResumeRun={resumeRun}
                            pastRuns={pastRuns}
                            onRestoreRun={restoreRun}
                        />
                    </div>

                    {/* Response detail modal */}
                    {responseModal && (
                        <ResponseModal
                            stepName={responseModal.step_name}
                            stepType={responseModal.step_type}
                            content={responseModal.content}
                            onClose={() => setResponseModal(null)}
                        />
                    )}
                </div>
            )}

            <ConfirmationModal
                isOpen={!!confirmDeleteOrchId}
                title="Delete Orchestration"
                message="Are you sure you want to delete this orchestration? This action cannot be undone."
                onConfirm={() => {
                    confirmDeleteOrchestration();
                    setConfirmDeleteOrchId(null);
                }}
                onClose={() => setConfirmDeleteOrchId(null)}
            />

            <ConfirmationModal
                isOpen={builderSavedOrch !== null}
                title="The builder changed this orchestration"
                message="You have unsaved edits open. Load the builder's version and lose them, or keep editing yours?"
                confirmText="Load the builder's version"
                onConfirm={() => {
                    const orch = builderSavedOrch;
                    setBuilderSavedOrch(null);
                    if (orch) void loadSavedOrchestration(orch);
                }}
                onClose={() => setBuilderSavedOrch(null)}
            />

        </div>
    );
}

// --- Response detail modal ---
// The full document treatment — styled headings, lists, tables, code — shared
// by every modal that shows model-written markdown. The human-input context
// used to render through a four-entry map at text-xs, which flattened a
// report's headings into cramped body text while the agent-response modal
// next to it looked finished.

function ResponseModal({ stepName, stepType, content, onClose }: { stepName: string; stepType?: string; content: string; onClose: () => void }) {
    // Try to pretty-print JSON for extract_json steps
    const isJson = stepType === 'extract_json';
    let formattedJson = content;
    let jsonParseOk = false;
    if (isJson) {
        try {
            const parsed = JSON.parse(content);
            formattedJson = JSON.stringify(parsed, null, 2);
            jsonParseOk = true;
        } catch {
            // content may already be pretty or non-JSON — just display as-is
            formattedJson = content;
        }
    }
    const [copied, setCopied] = useState(false);
    useEffect(() => {
        const handler = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
        document.addEventListener('keydown', handler);
        return () => document.removeEventListener('keydown', handler);
    }, [onClose]);

    return createPortal(
        <div
            className="fixed inset-0 z-[9999] flex items-center justify-center p-4"
            onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}
        >
            {/* Backdrop */}
            <div className="absolute inset-0 bg-scrim/70 backdrop-blur-sm" />
            {/* Panel */}
            <div className="relative z-10 w-full max-w-3xl max-h-[80vh] flex flex-col rounded-xl border border-zinc-700 bg-zinc-900 shadow-2xl">
                {/* Header */}
                <div className="flex items-center justify-between px-5 py-3 border-b border-zinc-700 shrink-0">
                    <div className="flex items-center gap-2">
                        <Bot size={14} className="text-blue-400" />
                        <span className="text-sm font-semibold text-zinc-100">{stepName}</span>
                        <span className="text-xs text-text-faint">— full response</span>
                    </div>
                    <div className="flex items-center gap-1">
                        <button
                            onClick={() => { navigator.clipboard.writeText(isJson ? formattedJson : content); setCopied(true); setTimeout(() => setCopied(false), 2000); }}
                            className="text-zinc-400 hover:text-zinc-100 transition-colors p-1 rounded-md hover:bg-zinc-700"
                            title="Copy to clipboard"
                        >
                            {copied ? <Check size={15} className="text-green-400" /> : <Copy size={15} />}
                        </button>
                        <button onClick={onClose} className="text-zinc-400 hover:text-zinc-100 transition-colors p-1 rounded-md hover:bg-zinc-700">
                            <X size={15} />
                        </button>
                    </div>
                </div>
                {/* Content */}
                <div className="flex-1 overflow-y-auto p-5">
                    {isJson ? (
                        /* Pretty-printed JSON with basic syntax colouring */
                        <pre className="text-xs font-code leading-relaxed whitespace-pre-wrap break-all rounded-lg bg-zinc-800/70 border border-zinc-700/50 p-4 overflow-x-auto">
                            {(jsonParseOk ? formattedJson : content)
                                .split('\n')
                                .map((line, i) => {
                                    /* Colour keys orange, strings lime, numbers/booleans/null cyan */
                                    const coloured = line
                                        .replace(/("(?:[^"\\]|\\.)*")(\s*:)/g, '<span class="text-orange-300">$1</span><span class="text-zinc-400">$2</span>')
                                        .replace(/:\s*("(?:[^"\\]|\\.)*")/g, ': <span class="text-lime-300">$1</span>')
                                        .replace(/:\s*(\d+\.?\d*)/g, ': <span class="text-cyan-300">$1</span>')
                                        .replace(/:\s*(true|false|null)\b/g, ': <span class="text-cyan-400">$1</span>');
                                    return (
                                        <span key={i} dangerouslySetInnerHTML={{ __html: coloured + '\n' }} />
                                    );
                                })}
                        </pre>
                    ) : (
                    <Markdown className="max-w-none">{content}</Markdown>
                    )}
                </div>
            </div>
        </div>,
        document.body
    );
}

// --- Bottom panel with collapsible sections ---
function BottomPanel({
    draft, setDraft, runStatus, liveActivity, runLog, runInput, setRunInput, onStartRun,
    humanPrompt, humanContext, humanResponse, setHumanResponse, onSubmitHuman, onOpenResponseModal,
    humanFields, humanValues, setHumanValues,
    runId, onResumeRun, pastRuns, onRestoreRun, rubrics,
}: {
    draft: Orchestration;
    setDraft: (o: Orchestration) => void;
    runStatus: string;
    liveActivity: string | null;
    runLog: LogEntry[];
    runInput: string;
    setRunInput: (v: string) => void;
    onStartRun: () => void;
    humanPrompt: string | null;
    humanContext: string | null;
    humanResponse: string;
    setHumanResponse: (v: string) => void;
    humanFields: HumanField[];
    humanValues: HumanValues;
    setHumanValues: (v: HumanValues) => void;
    onSubmitHuman: () => void;
    onOpenResponseModal: (entry: { step_name: string; step_type?: string; content: string }) => void;
    runId: string | null;
    onResumeRun: () => void;
    pastRuns: { run_id: string; orchestration_id: string; status: string; started_at?: string | null; ended_at?: string | null }[];
    onRestoreRun: (run: { run_id: string; orchestration_id: string; status: string }) => void;
    rubrics: { id: string; name: string }[];
}) {
    const [activeSection, setActiveSection] = useState<'state' | 'guardrails' | 'run' | 'recent'>('run');
    const [panelHeight, setPanelHeight] = useState(280);
    // The question opens in a modal (the agent-response pattern): on a short
    // log area the inline card, context and all, swallowed the whole tab. The
    // sticky strip below is the trigger and stays one line tall. A fresh
    // prompt closes a stale modal — reset during render (the sanctioned
    // "adjust state when a prop changes" pattern) rather than in an effect.
    const [humanModalOpen, setHumanModalOpen] = useState(false);
    const [seenPrompt, setSeenPrompt] = useState(humanPrompt);
    if (seenPrompt !== humanPrompt) {
        setSeenPrompt(humanPrompt);
        setHumanModalOpen(false);
    }
    // The panel's one scroller, shared by all four sections. The run log
    // follows the newest line only while the reader is at its foot (F127):
    // the old effect set `scrollTop` on the log's own div, which does not
    // scroll, so the log never followed at all. Scrolling up to read stops the
    // following; "Jump to latest" starts it again, as cloud's RunPanel does.
    const scrollRef = useRef<HTMLDivElement>(null);
    const [pinned, setPinned] = useState(true);
    if (runLog.length === 0 && !pinned) setPinned(true); // a new run starts at its foot
    const dragRef = useRef<{ startY: number; startHeight: number } | null>(null);

    const onDragHandleMouseDown = useCallback((e: React.MouseEvent) => {
        e.preventDefault();
        dragRef.current = { startY: e.clientY, startHeight: panelHeight };
        const onMouseMove = (ev: MouseEvent) => {
            if (!dragRef.current) return;
            const delta = dragRef.current.startY - ev.clientY;
            const newHeight = Math.max(120, Math.min(700, dragRef.current.startHeight + delta));
            setPanelHeight(newHeight);
        };
        const onMouseUp = () => {
            dragRef.current = null;
            document.removeEventListener('mousemove', onMouseMove);
            document.removeEventListener('mouseup', onMouseUp);
        };
        document.addEventListener('mousemove', onMouseMove);
        document.addEventListener('mouseup', onMouseUp);
    }, [panelHeight]);

    useEffect(() => {
        const el = scrollRef.current;
        if (el && pinned && activeSection === 'run') el.scrollTop = el.scrollHeight;
    }, [runLog, pinned, activeSection]);

    const onPanelScroll = () => {
        const el = scrollRef.current;
        if (el && activeSection === 'run') setPinned(el.scrollHeight - el.scrollTop - el.clientHeight < 24);
    };

    return (
        <div style={{ height: panelHeight }} className="flex flex-col">
            {/* Drag handle */}
            <div
                onMouseDown={onDragHandleMouseDown}
                className="h-1.5 w-full cursor-row-resize bg-zinc-800 hover:bg-blue-500/40 transition-colors flex-shrink-0 group flex items-center justify-center"
            >
                <div className="w-8 h-0.5 rounded-md bg-zinc-600 group-hover:bg-blue-400 transition-colors" />
            </div>
            {/* Section tabs */}
            <div className="flex border-b border-zinc-800 flex-shrink-0">
                {(['state', 'guardrails', 'run'] as const).map(section => (
                    <button
                        key={section}
                        onClick={() => setActiveSection(section)}
                        className={`px-4 py-2 text-xs font-medium capitalize transition-colors ${
                            activeSection === section
                                ? 'text-blue-400 border-b-2 border-blue-400'
                                : 'text-text-faint hover:text-zinc-300'
                        }`}
                    >
                        {section === 'state' ? 'State Schema' : section === 'guardrails' ? 'Guardrails' : 'Run Log'}
                        {section === 'run' && runStatus !== 'idle' && (
                            <span className={`ml-2 inline-block w-2 h-2 rounded-full ${
                                runStatus === 'running' ? 'bg-blue-400 animate-pulse' :
                                runStatus === 'completed' ? 'bg-green-400' :
                                runStatus === 'paused' ? 'bg-yellow-400' :
                                runStatus === 'cancelled' ? 'bg-zinc-500' : 'bg-red-400'
                            }`} />
                        )}
                    </button>
                ))}
                <button
                    onClick={() => setActiveSection('recent')}
                    className={`px-4 py-2 text-xs font-medium transition-colors ${
                        activeSection === 'recent'
                            ? 'text-blue-400 border-b-2 border-blue-400'
                            : 'text-text-faint hover:text-zinc-300'
                    }`}
                >
                    Recent Runs
                    {pastRuns.length > 0 && (
                        <span className="ml-1.5 text-[10px] bg-surface-2 text-text-muted rounded-md px-1.5 py-0.5">
                            {pastRuns.length}
                        </span>
                    )}
                </button>
            </div>

            <div ref={scrollRef} onScroll={onPanelScroll} className="p-4 flex-1 overflow-y-auto min-h-0">
                {/* State Schema */}
                {activeSection === 'state' && (
                    <StateSchemaEditor
                        schema={draft.state_schema}
                        onChange={(schema) => setDraft({ ...draft, state_schema: schema })}
                    />
                )}

                {/* Guardrails */}
                {activeSection === 'guardrails' && (
                    <div className="grid grid-cols-3 gap-4">
                        <div>
                            <label htmlFor="orch-max-turns" className="text-xs text-zinc-400 block mb-1">Max Total Turns</label>
                            <input
                                id="orch-max-turns"
                                type="number"
                                className="w-full bg-zinc-800 border border-zinc-700 rounded-md px-3 py-1.5 text-sm text-zinc-200 outline-none"
                                value={draft.max_total_turns}
                                onChange={(e) => setDraft({ ...draft, max_total_turns: parseInt(e.target.value) || 100 })}
                            />
                        </div>
                        <div>
                            <label htmlFor="orch-timeout" className="text-xs text-zinc-400 block mb-1">Timeout (minutes)</label>
                            <input
                                id="orch-timeout"
                                type="number"
                                className="w-full bg-zinc-800 border border-zinc-700 rounded-md px-3 py-1.5 text-sm text-zinc-200 outline-none"
                                value={draft.timeout_minutes}
                                onChange={(e) => setDraft({ ...draft, timeout_minutes: parseInt(e.target.value) || 30 })}
                            />
                        </div>
                        {/* No "Max Cost (USD)": nothing ever enforced it, and batch 1C
                            deleted it from the engine. A cap that does not cap is
                            worse than none — whoever set it expected a runaway run
                            to stop (F76). */}
                        <div className="col-span-3">
                            <label className="flex items-start gap-2 cursor-pointer">
                                <input
                                    type="checkbox"
                                    className="mt-0.5"
                                    checked={!!draft.strict_state_types}
                                    onChange={(e) => setDraft({ ...draft, strict_state_types: e.target.checked })}
                                />
                                <span>
                                    <span className="text-xs text-zinc-400 block">Stop on a state type mismatch</span>
                                    <span className="text-[11px] text-text-faint block mt-0.5">
                                        A step&apos;s output is read as the type its state key declares —
                                        JSON text becomes a list or an object. Off, a step whose text will
                                        not convert leaves the text in place and warns. On, the run stops
                                        there instead of carrying on with the wrong shape.
                                    </span>
                                </span>
                            </label>
                        </div>
                        <div className="col-span-3">
                            <label htmlFor="orch-rubric" className="text-xs text-zinc-400 block mb-1">Score runs against</label>
                            <select
                                id="orch-rubric"
                                className="w-full bg-zinc-800 border border-zinc-700 rounded-md px-3 py-1.5 text-sm text-zinc-200 outline-none"
                                value={draft.rubric_id ?? ''}
                                onChange={(e) => setDraft({ ...draft, rubric_id: e.target.value })}
                            >
                                <option value="">Not evaluated</option>
                                {rubrics.map((r) => (
                                    <option key={r.id} value={r.id}>{r.name}</option>
                                ))}
                            </select>
                            <div className="text-[11px] text-text-faint mt-1">
                                {draft.rubric_id
                                    ? 'Every run is judged when it finishes. The judge is a model call, billed like any other.'
                                    : 'Pick a rubric to have an LLM judge score every run and flag regressions. Rubrics are written on the Evals screen.'}
                            </div>
                        </div>
                    </div>
                )}

                {/* Recent Runs */}
                {activeSection === 'recent' && (
                    <div className="space-y-1">
                        {pastRuns.length === 0 ? (
                            <div className="text-text-faint italic text-xs">No runs yet.</div>
                        ) : (
                            pastRuns.slice(0, 20).map(r => (
                                <div key={r.run_id} className="flex items-center gap-2 text-[11px] py-1 px-2 rounded-md bg-zinc-800/50">
                                    <span className={`w-1.5 h-1.5 rounded-full flex-shrink-0 ${
                                        r.status === 'completed' ? 'bg-green-400' :
                                        r.status === 'failed'    ? 'bg-red-400' :
                                        r.status === 'cancelled' ? 'bg-zinc-500' :
                                        r.status === 'paused'    ? 'bg-yellow-400' : 'bg-blue-400 animate-pulse'
                                    }`} />
                                    <span className="text-zinc-400 truncate flex-1" title={r.run_id}>{r.run_id}</span>
                                    <span className="text-text-faint capitalize">{r.status}</span>
                                    {r.started_at && (
                                        <span className="text-text-faint text-[10px]">
                                            {new Date(r.started_at).toLocaleTimeString()}
                                        </span>
                                    )}
                                    {(r.status === 'failed' || r.status === 'cancelled') && (
                                        <button
                                            onClick={() => { onRestoreRun(r); setActiveSection('run'); }}
                                            className="px-2 py-0.5 text-[10px] bg-orange-600 hover:bg-orange-500 text-white rounded-md"
                                        >
                                            Resume
                                        </button>
                                    )}
                                </div>
                            ))
                        )}
                    </div>
                )}

                {/* Run Log */}
                {activeSection === 'run' && (
                    <div className="space-y-2">
                        {/* Input bar */}
                        {(runStatus === 'idle' || runStatus === 'completed' || runStatus === 'failed' || runStatus === 'cancelled') && (
                            <div className="flex gap-2">
                                <input
                                    aria-label="Initial input for this run"
                                    className="flex-1 bg-zinc-800 border border-zinc-700 rounded-md px-3 py-1.5 text-xs text-zinc-200 outline-none"
                                    value={runInput}
                                    onChange={(e) => setRunInput(e.target.value)}
                                    placeholder="Initial input for the orchestration..."
                                    onKeyDown={(e) => { if (e.key === 'Enter') onStartRun(); }}
                                />
                                {(runStatus === 'failed' || runStatus === 'cancelled') && runId && (
                                    <button
                                        onClick={onResumeRun}
                                        className="px-3 py-1.5 text-xs bg-orange-600 hover:bg-orange-500 text-white rounded-md whitespace-nowrap"
                                    >
                                        Resume from failure
                                    </button>
                                )}
                            </div>
                        )}

                        {/* Live activity: what the model is doing right now */}
                        {runStatus === 'running' && liveActivity && (
                            <div className="flex items-center gap-2 text-[11px] text-sky-300/90 bg-sky-950/30 border border-sky-900/40 rounded-md px-2.5 py-1.5">
                                <Spinner size={16} className="shrink-0" />
                                <span className="truncate">{liveActivity}</span>
                            </div>
                        )}

                        {/* Log output */}
                        <div className="font-mono text-[11px] text-zinc-400 space-y-0.5">
                            {runLog.length === 0 ? (
                                <div className="text-text-faint italic">No run output yet. Click Run to start.</div>
                            ) : (
                                runLog.map((entry, i) => {
                                    if (typeof entry !== 'string') {
                                        if (entry.kind === 'line') {
                                            const meta = LINE_META[entry.tone];
                                            const LineIcon = meta.icon;
                                            return (
                                                <div key={i} className={`flex items-start gap-1.5 ${meta.cls}`}>
                                                    <LineIcon size={11} className="mt-[3px] shrink-0" aria-hidden />
                                                    {/* Plain text, not markdown. These lines are built
                                                        above out of event fields — an expression, a step
                                                        name, a tool name, a customer's own data — and none
                                                        of them is markdown anyone wrote. Rendering them as
                                                        markdown ate the underscores in
                                                        `().__class__.__bases__[0].__subclasses__()`, so a
                                                        run log explaining which attribute the engine
                                                        refused named a different attribute. */}
                                                    <span className="min-w-0 flex-1 whitespace-pre-wrap break-words">{entry.text}</span>
                                                </div>
                                            );
                                        }
                                        if (entry.kind === 'tool_call') {
                                            return (
                                                <div key={i} className="text-accent pl-2">
                                                    <details>
                                                        <summary className="cursor-pointer list-none">
                                                            <Wrench size={11} className="mr-1 inline-block align-[-1px]" aria-hidden />
                                                            {entry.tool_name}
                                                            {entry.step_name && <span className="text-text-faint text-[10px]"> · {entry.step_name}</span>}
                                                        </summary>
                                                        <pre className="font-code bg-zinc-800/50 p-1 rounded-md mt-0.5 text-[10px] text-zinc-300 overflow-x-auto whitespace-pre-wrap">
                                                            {JSON.stringify(entry.args, null, 2)}
                                                        </pre>
                                                    </details>
                                                </div>
                                            );
                                        }
                                        if (entry.kind === 'tool_result') {
                                            return (
                                                <div key={i} className="flex items-start gap-1.5 pl-4 text-[10px] text-text-faint">
                                                    <CornerDownRight size={10} className="mt-[2px] shrink-0" aria-hidden />
                                                    <span className="min-w-0 flex-1">{entry.preview.slice(0, 200)}{entry.preview.length > 200 ? '…' : ''}</span>
                                                </div>
                                            );
                                        }
                                        if (entry.kind === 'step_result') {
                                            const preview = entry.content.slice(0, 200).replace(/\n+/g, ' ').trim();
                                            const isTruncated = entry.content.length > 200;
                                            const isAgent = entry.step_type === 'agent';
                                            const isPrint = entry.step_type === 'print';
                                            const isExtractJson = entry.step_type === 'extract_json';
                                            const dotColor = isAgent ? 'bg-emerald-400' : isPrint ? 'bg-lime-400' : isExtractJson ? 'bg-orange-400' : 'bg-teal-400';
                                            const labelColor = isAgent ? 'text-emerald-400' : isPrint ? 'text-lime-400' : isExtractJson ? 'text-orange-400' : 'text-teal-400';
                                            const typeLabel = isAgent ? 'agent response' : isPrint ? 'print output' : isExtractJson ? 'json output' : 'llm output';
                                            return (
                                                <div key={i} className="my-1.5">
                                                    {/* Header label */}
                                                    <div className="flex items-center gap-1.5 mb-1 pl-1">
                                                        <div className={`w-1.5 h-1.5 rounded-full flex-shrink-0 ${dotColor}`} />
                                                        <span className={`text-[10px] font-semibold uppercase tracking-wider ${labelColor}`}>
                                                            {entry.step_name}
                                                        </span>
                                                        <span className="text-[9px] text-text-faint uppercase tracking-wide">
                                                            {typeLabel}
                                                        </span>
                                                    </div>
                                                    {/* Content bubble \u2014 the whole card opens the full
                                                        response; the hover hint is just the signpost. */}
                                                    <button
                                                        type="button"
                                                        onClick={() => onOpenResponseModal({ step_name: entry.step_name, step_type: entry.step_type, content: entry.content })}
                                                        title="View full response"
                                                        className="group ml-3 flex w-[calc(100%-0.75rem)] items-start gap-2 rounded-lg border border-zinc-700/50 bg-zinc-800/70 px-3 py-2 text-left transition-colors hover:border-zinc-600 hover:bg-zinc-800"
                                                    >
                                                        <span className="min-w-0 flex-1 leading-relaxed text-zinc-300">
                                                            {preview}{isTruncated ? '\u2026' : ''}
                                                        </span>
                                                        <span className="ml-1 flex shrink-0 items-center gap-1 self-center whitespace-nowrap text-[10px] text-text-faint opacity-0 transition-opacity group-hover:opacity-100">
                                                            <ExternalLink size={10} aria-hidden />
                                                            View full
                                                        </span>
                                                    </button>
                                                </div>
                                            );
                                        }
                                        if (entry.kind === 'tasks') {
                                            const done = entry.tasks.filter(t => t.status === 'completed').length;
                                            return (
                                                <div key={i} className="my-1.5 ml-1 rounded-lg border border-zinc-700/50 bg-zinc-800/40 px-3 py-2">
                                                    <div className="flex items-center gap-1.5 mb-1">
                                                        <ListTodo size={11} className="text-accent shrink-0" aria-hidden />
                                                        <span className="text-[10px] font-semibold uppercase tracking-wider text-zinc-400">Tasks</span>
                                                        <span className="text-[9px] text-text-faint">
                                                            {done}/{entry.tasks.length} done{entry.step_name ? ` · ${entry.step_name}` : ''}
                                                        </span>
                                                    </div>
                                                    <ul className="space-y-0.5">
                                                        {entry.tasks.map((t, j) => (
                                                            <li key={j} className="flex items-start gap-1.5 text-[11px] leading-snug">
                                                                {t.status === 'completed' ? (
                                                                    <CircleCheck size={11} className="mt-[2px] shrink-0 text-emerald-400" aria-hidden />
                                                                ) : t.status === 'in_progress' ? (
                                                                    <CircleDot size={11} className="mt-[2px] shrink-0 text-accent" aria-hidden />
                                                                ) : t.status === 'cancelled' ? (
                                                                    <CircleX size={11} className="mt-[2px] shrink-0 text-text-faint" aria-hidden />
                                                                ) : (
                                                                    <Circle size={11} className="mt-[2px] shrink-0 text-text-faint" aria-hidden />
                                                                )}
                                                                <span className={
                                                                    t.status === 'completed' ? 'text-text-faint line-through' :
                                                                    t.status === 'in_progress' ? 'text-zinc-200' :
                                                                    t.status === 'cancelled' ? 'text-text-faint line-through' :
                                                                    'text-zinc-400'
                                                                }>{t.content}</span>
                                                            </li>
                                                        ))}
                                                    </ul>
                                                </div>
                                            );
                                        }
                                        if (entry.kind === 'reasoning' || entry.kind === 'thought') {
                                            const isReasoning = entry.kind === 'reasoning';
                                            const firstLine = entry.content.split('\n')[0].slice(0, 120);
                                            return (
                                                <div key={i} className="pl-2 my-0.5">
                                                    <details>
                                                        <summary className={`cursor-pointer list-none text-[10px] ${isReasoning ? 'text-sky-400/80' : 'text-text-faint'}`}>
                                                            {isReasoning
                                                                ? <Brain size={10} className="mr-1 inline-block align-[-1px]" aria-hidden />
                                                                : <MessageSquare size={10} className="mr-1 inline-block align-[-1px]" aria-hidden />}
                                                            {firstLine}{entry.content.length > firstLine.length ? '…' : ''}
                                                            {entry.step_name && <span className="text-text-faint"> · {entry.step_name}</span>}
                                                        </summary>
                                                        <div className="mt-0.5 bg-zinc-800/40 border border-zinc-800 rounded-md p-2 text-[10px] text-zinc-400 whitespace-pre-wrap max-h-64 overflow-y-auto">
                                                            {entry.content}
                                                        </div>
                                                    </details>
                                                </div>
                                            );
                                        }
                                        return null;
                                    }
                                    return (
                                        <div key={i} className={
                                            entry.startsWith('✓') ? 'text-green-400' :
                                            entry.startsWith('✗') ? 'text-red-400' :
                                            entry.startsWith('▶') ? 'text-blue-400' :
                                            entry.startsWith('⏸') ? 'text-amber-400' :
                                            entry.startsWith('⟳') ? 'text-accent' :
                                            'text-zinc-400'
                                        }>
                                            <ReactMarkdown
                                                remarkPlugins={[remarkGfm]}
                                                components={{
                                                    p: ({ children }) => <span>{children}</span>,
                                                    code: ({ children }) => <code className="font-code bg-surface-2 px-1 rounded-md text-[10px]">{children}</code>,
                                                    pre: ({ children }) => <pre className="font-code bg-surface-2 p-1 rounded-md mt-0.5 overflow-x-auto">{children}</pre>,
                                                    a: ({ href, children }) => <a href={href} className="underline opacity-70" target="_blank" rel="noreferrer">{children}</a>,
                                                }}
                                            >{entry}</ReactMarkdown>
                                        </div>
                                    );
                                })
                            )}
                        </div>

                        {/* Centred, not in the bottom-right corner the builder launcher
                            owns (F37). Not while a question is up: the run is waiting,
                            nothing new arrives, and the strip below says where it is. */}
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

                        {/* Human input — a slim sticky strip as the trigger, with the
                            question, context and answer in a modal (the same pattern
                            as the agent-response bubble). On a short log area the
                            inline card swallowed the whole tab. */}
                        {/* Paused as well as asking (F141): a cancelled run kept its
                            prompt, so this strip stayed up offering an Answer the API
                            refuses with a 409 — and an in-process cancel emits no
                            event, so nothing cleared it. Cloud's run page has checked
                            the status since F136. */}
                        {humanPrompt && runStatus === 'paused' && (
                            <div className="sticky bottom-0 z-10 pt-2">
                                {/* pr-16 keeps Answer out of the bottom-right corner the
                                    builder launcher owns (44px at a 20px inset, fixed).
                                    Once the log is long enough this strip reaches the
                                    foot of the viewport, and the launcher sat on top of
                                    the one control that answers the run. */}
                                <button
                                    type="button"
                                    onClick={() => setHumanModalOpen(true)}
                                    className="group flex w-full items-center gap-2 rounded-lg border border-amber-600/50 bg-zinc-900 py-2 pl-3 pr-16 text-left shadow-[0_-12px_28px_-8px_rgba(0,0,0,0.55)] transition-colors hover:border-amber-500"
                                >
                                    <span className="relative flex size-2 shrink-0">
                                        <span className="absolute inline-flex size-full animate-ping rounded-full bg-amber-400 opacity-60" aria-hidden />
                                        <span className="relative inline-flex size-2 rounded-full bg-amber-400" aria-hidden />
                                    </span>
                                    <span className="shrink-0 text-xs font-semibold text-amber-300">Waiting for your input</span>
                                    <span className="min-w-0 flex-1 truncate text-[11px] text-amber-200">{humanPrompt}</span>
                                    <span className="shrink-0 rounded-md bg-amber-500 px-2.5 py-1 text-[11px] font-medium text-zinc-950 transition-colors group-hover:bg-amber-400">
                                        Answer
                                    </span>
                                </button>
                            </div>
                        )}

                        <Modal
                            open={!!humanPrompt && runStatus === 'paused' && humanModalOpen}
                            onClose={() => setHumanModalOpen(false)}
                            title="Human input required"
                            size="lg"
                            footer={
                                <>
                                    <Button variant="secondary" onClick={() => setHumanModalOpen(false)}>Later</Button>
                                    <Button onClick={onSubmitHuman} disabled={!humanFields.length && !humanResponse.trim()}>Submit</Button>
                                </>
                            }
                        >
                            <div className="space-y-3">
                                {humanContext && (
                                    <div className="max-h-[45vh] overflow-y-auto rounded-md border border-border bg-surface-2 p-4">
                                        <Markdown>{humanContext}</Markdown>
                                    </div>
                                )}
                                <div className="text-sm leading-relaxed text-amber-300">
                                    <ReactMarkdown
                                        remarkPlugins={[remarkGfm]}
                                        components={{
                                            p: ({ children }) => <p className="mb-0">{children}</p>,
                                            a: ({ href, children }) => <a href={href} className="text-amber-200 underline" target="_blank" rel="noreferrer">{children}</a>,
                                            strong: ({ children }) => <strong className="font-semibold text-amber-200">{children}</strong>,
                                        }}
                                    >{humanPrompt}</ReactMarkdown>
                                </div>
                                {humanFields.length > 0 ? (
                                    <HumanFieldsForm fields={humanFields} values={humanValues} onChange={setHumanValues} idPrefix="orch-answer" />
                                ) : (
                                    <input
                                        autoFocus
                                        aria-label="Your answer"
                                        className="w-full rounded-md border border-amber-700/40 bg-zinc-900/80 px-3 py-2 text-sm text-zinc-100 outline-none transition-colors placeholder:text-zinc-500 focus:border-amber-500"
                                        value={humanResponse}
                                        onChange={(e) => setHumanResponse(e.target.value)}
                                        placeholder="Your response…"
                                        onKeyDown={(e) => { if (e.key === 'Enter') onSubmitHuman(); }}
                                    />
                                )}
                            </div>
                        </Modal>

                    </div>
                )}
            </div>
        </div>
    );
}
