'use client';
/* eslint-disable @typescript-eslint/no-explicit-any */
import { useState, useEffect, useEffectEvent, useCallback, useRef, useSyncExternalStore } from 'react';
import { Plus, Trash, Copy, Radio, Bot } from 'lucide-react';
import { Combobox, ConfirmDialog, Mark, SearchInput } from '@/components/ui';
import { useRouter } from 'next/navigation';
import { matchesQuery } from '@/lib/search';
import { RESOURCE_SAVED_EVENT, useBuilder } from '@/components/app/BuilderProvider';
import { cloneStep, generateStepId, removeStepFromGraph } from '../orchestration/graph';
import { validateOrchestration } from '../orchestration/validate';
import { useBuilderShortcuts, useDraftHistory } from '../orchestration/use-draft-history';
import { readWithStallTimeout } from '@/lib/sse';
import { refusedAnswerMessage } from '@/lib/answers';
import { failureReason, stepErrorsFrom } from '@/lib/runErrors';
import { CanvasWorkbench } from '../orchestration/CanvasWorkbench';
import { StepConfigPanel } from '../orchestration/StepConfigPanel';
import { StateSchemaEditor } from '../orchestration/StateSchemaEditor';
import {
    DescriptionField, GuardrailsFields, SchedulesList, SettingsSection, WorkflowSettingsPanel,
} from '../orchestration/WorkflowSettings';
import { humanAnswer, initialHumanValues, type HumanField, type HumanValues } from '../orchestration/humanFields';
import type { Orchestration, StepConfig } from '@/types/orchestration';
import type { KnowledgeBaseOption } from '../orchestration/panel/shared';
import { knowledgeApi, useKnowledgeEnabled } from '@/lib/knowledge';
import { useHelpTopic } from '@/components/help';
import { ConfirmationModal } from './ConfirmationModal';
import { ToastNotification } from './ToastNotification';
import { isLiveRun, runStatusMeta, type RunSummary } from '@/components/runs/summary';
import { RunList, type RunRow } from '@/components/run';
import { ossRunRow } from '@/lib/runHost';
import {
    line, ResponseModal, type LogEntry, type ReasoningLogEntry, type RetrievalLogEntry, type StepResultLogEntry,
    type TasksLogEntry, type ThoughtLogEntry, type ToolCallLogEntry, type ToolResultLogEntry,
} from '@/components/workflows/runLog';
import { RunDrawer } from '@/components/workflows/RunDrawer';
import { RunStrip, type RunDrawerView } from '@/components/workflows/RunStrip';

type RunStepStatus = 'pending' | 'running' | 'paused' | 'completed' | 'failed';

// Pure reducer: mark each step present in a persisted checkpoint's step_history
// as completed/failed. Shared by restoreRun and reattachRun.
function applyStepHistory(
    prev: Record<string, RunStepStatus>,
    stepHistory: any[],
): Record<string, RunStepStatus> {
    const next = { ...prev };
    for (const h of stepHistory) {
        if (!h?.step_id) continue;
        // A question closed by a cancel was never answered: not green (F193).
        next[h.step_id] = h.status === 'failed' ? 'failed' : h.status === 'cancelled' ? 'pending' : 'completed';
    }
    return next;
}

// A run that has ended asks nothing and runs nothing. A cancelled one's
// question stops waiting (F193); and no step keeps pulsing: one still marked
// waiting or running is done if the run completed, and not reached otherwise
// (F190). End, for one, never gets a step_complete of its own.
function settleRun(
    prev: Record<string, RunStepStatus>,
    outcome: 'completed' | 'failed' | 'cancelled',
): Record<string, RunStepStatus> {
    if (!Object.values(prev).some(v => v === 'paused' || v === 'running')) return prev;
    const to: RunStepStatus = outcome === 'completed' ? 'completed' : 'pending';
    const next = { ...prev };
    for (const k in next) { if (next[k] === 'paused' || next[k] === 'running') next[k] = to; }
    return next;
}

// Every cancel says this on a run's stream: the pump, the fleet and `cancel_run`.
const isCancel = (event: { type?: string; error?: unknown }) =>
    event?.type === 'orchestration_error' && event.error === 'Cancelled';

const CANCELLED_LINE = '[Cancelled]';

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
    result_key: '',
};

/** Step events a nested run forwards that belong to its own steps (F251). */
const NESTED_STEP_EVENTS = new Set(['step_start', 'step_complete', 'step_error', 'final',
    'tasks_updated', 'orchestration_error']);

function subscribeResize(onChange: () => void) {
    window.addEventListener('resize', onChange);
    return () => window.removeEventListener('resize', onChange);
}
const readWindowWidth = () => window.innerWidth;
const serverWindowWidth = () => 0;

/** A human step's declared fields off the wire — anything else is no fields (F75). */
function fieldList(raw: unknown): HumanField[] {
    return Array.isArray(raw) ? (raw as HumanField[]) : [];
}

export function OrchestrationTab({ initialRunId, initialOrchId, newRequest }: {
    initialRunId?: string;
    initialOrchId?: string;
    /** A value that changes each time a blank workflow is asked for from elsewhere (⌘K's New workflow, M1a). */
    newRequest?: string;
} = {}) {
    // --- Orchestration list ---
    const [orchestrations, setOrchestrations] = useState<Orchestration[]>([]);
    const [orchQuery, setOrchQuery] = useState('');
    const [selectedOrchId, setSelectedOrchId] = useState<string | null>(null);
    // The draft with undo/redo, dirty tracking and the unload guard.
    // `setDraft` records history (edits); `replaceDraft` resets it (navigation).
    const { draft, setDraft, replaceDraft, markSaved, undo, redo, canUndo, canRedo, dirty } = useDraftHistory();
    const router = useRouter();
    // The "?" panel explains the editor while one is open, the list otherwise (H1).
    useHelpTopic(draft ? 'orchestrations.editor' : null);

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
    // Knowledge (R1): its Retrieve and Ingest steps are offered only while it is
    // on, and their pickers list the workspace's knowledge bases.
    const knowledgeEnabled = useKnowledgeEnabled() === true;
    const [knowledgeBases, setKnowledgeBases] = useState<KnowledgeBaseOption[] | undefined>(undefined);
    useEffect(() => {
        if (!knowledgeEnabled) return;
        let live = true;
        knowledgeApi.bases()
            .then((bases) => { if (live) setKnowledgeBases(bases.map(({ id, name, status, description }) => ({ id, name, status, description }))); })
            .catch(() => { /* the pickers fall back to typed ids */ });
        return () => { live = false; };
    }, [knowledgeEnabled]);
    const [availableModels, setAvailableModels] = useState<string[]>([]);
    // What a step's semantic cache embeds with (F327): the panel says whether one is set.
    const [embeddingModel, setEmbeddingModel] = useState<string | null>(null);
    useEffect(() => {
        let live = true;
        fetch('/api/settings').then(r => r.json())
            .then((s) => { if (live) setEmbeddingModel(String(s?.embedding_model || '')); })
            .catch(() => { /* the panel keeps its general note */ });
        return () => { live = false; };
    }, []);
    const semanticCacheNote = embeddingModel === null ? undefined : embeddingModel
        ? `It embeds each prompt with ${embeddingModel}, the Embedding Model in Settings → Models.`
        : 'No Embedding Model is set in Settings → Models, so only exact match runs.';
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
    // The step whose question is open, so the step that starts after the answer
    // can close it (F190).
    const askedStepRef = useRef<string | null>(null);
    // Why each failed step failed, in its own words (F195): shown on its card and in the run log.
    const [runStepErrors, setRunStepErrors] = useState<Record<string, string>>({});
    const [runId, setRunId] = useState<string | null>(null);
    const [runInput, setRunInput] = useState('');
    const [runLog, setRunLog] = useState<LogEntry[]>([]);
    const [humanPrompt, setHumanPrompt] = useState<string | null>(null);
    // What the question is about (the step's first input key with a value, or
    // the newest step output) and the key it came from, so its box can say what
    // it is (U65); a question from before the key was sent says "this run".
    const [humanContext, setHumanContext] = useState<{ text: string; key: string | null } | null>(null);
    const [humanResponse, setHumanResponse] = useState('');
    // The step's declared fields, when it has any — answered as a form keyed by
    // name, like every other place a human step is answered (F75). Set with the
    // prompt at each place a question arrives; ignored while there is none.
    const [humanFields, setHumanFields] = useState<HumanField[]>([]);
    const [humanValues, setHumanValues] = useState<HumanValues>({});
    const abortRef = useRef<AbortController | null>(null);
    // A reopened run's failed steps from its record, for a journal written
    // before `step_error` carried its reason (F195).
    const historyErrorsRef = useRef<Record<string, string>>({});
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
    // The open canvas's run drawer (M3a): the Run Log or Recent Runs, or shut.
    // A run started, resumed or reopened opens it on the log, where its lines arrive.
    const [runDrawer, setRunDrawer] = useState<RunDrawerView | null>(null);
    // Leaving an unsaved draft asks first (M3a, Naveen's OK): Back, the
    // switcher, New, another run, a link in the app. It used to drop it silently;
    // the browser's own prompt covered only a reload or a closed tab. The same
    // question as the agents screen's (F73).
    const [pendingLeave, setPendingLeave] = useState<null | (() => void)>(null);
    const [confirmDeleteOrchId, setConfirmDeleteOrchId] = useState<string | null>(null);
    const { open: openBuilder, setCurrentOrchestration, isOpen: builderOpen } = useBuilder();
    // The builder's panel stands at the window's right edge, 620 px wide at
    // most; the open canvas keeps its chrome clear of it while it is open.
    const windowWidth = useSyncExternalStore(subscribeResize, readWindowWidth, serverWindowWidth);
    const builderInset = builderOpen ? Math.min(620, windowWidth - 24) + 12 : 0;
    // Set when the builder saved the orchestration being edited while the draft
    // had unsaved changes — the user chooses which version survives.
    const [builderSavedOrch, setBuilderSavedOrch] = useState<any | null>(null);

    // --- Active runs (for reconnect banner) ---
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

    const thCls = 'px-4 py-2 text-[10px] font-medium uppercase tracking-wider text-text-faint whitespace-nowrap';
    const tdCls = 'px-4 py-2.5 text-xs';

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
        setRunDrawer('run');
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
        askedStepRef.current = null;
        historyErrorsRef.current = {};
        setRunStepErrors({});

        // Restore step statuses + human-input state from the persisted checkpoint
        // so the canvas is meaningful before (and in case of a pre-journal run,
        // without) the journal replay.
        let recorded: string | undefined;  // the run's status in its record
        try {
            const res = await fetch(`/api/orchestrations/runs/${runInfo.run_id}`);
            if (res.ok) {
                const data = await res.json();
                recorded = data.status;
                if (Array.isArray(data.step_history)) {
                    setRunStepStatuses(prev => applyStepHistory(prev, data.step_history));
                    historyErrorsRef.current = stepErrorsFrom(data.step_history);
                    setRunStepErrors(historyErrorsRef.current);
                }
                if (data.waiting_for_human && data.human_prompt) {
                    setHumanPrompt(data.human_prompt);
                    setHumanContext(data.human_context ? { text: String(data.human_context), key: null } : null);
                    setHumanFields(fieldList(data.human_fields));
                    setHumanValues(initialHumanValues(fieldList(data.human_fields)));
                }
            }
        } catch { /* ignore */ }

        // Full-fidelity replay + live tail from the event journal. Falls back to
        // status polling for runs that predate the journal.
        const hasJournal = await streamRunJournal(runInfo.run_id, 0);
        // The replay rebuilt the question a finished run once asked. A cancel
        // is in the journal since F193, but not in one written before it, and
        // the record is the authority either way: a finished run asks nothing.
        if (currentRunIdRef.current !== runInfo.run_id) return;  // switched runs meanwhile
        if (recorded === 'cancelled' || recorded === 'completed' || recorded === 'failed') {
            setHumanPrompt(null);
            setHumanContext(null);
            setRunStepStatuses(prev => settleRun(prev, recorded));
            setRunStatus(recorded);
        }
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

    // A row of the landing's run tables (the shared list, M2b) opens the run here, on the canvas.
    const openLandingRun = (row: RunRow) => restoreRun({ run_id: row.id, orchestration_id: row.workflowId, status: row.status });

    // --- Select orchestration ---
    const selectOrchestration = useCallback((id: string | null) => {
        setSelectedOrchId(id);
        setSelectedStepId(null);
        setRunStatus('idle');
        setRunStepStatuses({});
        setRunStepErrors({});
        historyErrorsRef.current = {};
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
    // A blank workflow nobody has touched yet holds nothing to lose: it asks only
    // once there is an edit (an undo step) or a saved workflow differs from its save.
    const hasEdits = !!draft && dirty && (canUndo || orchestrations.some(o => o.id === draft.id));
    const guardLeave = useCallback((go: () => void) => {
        if (hasEdits) setPendingLeave(() => go);
        else go();
    }, [hasEdits]);

    // A link inside the app gets the question too: the App Router cannot stop a
    // navigation once it has started, so the click is taken before it does.
    useEffect(() => {
        if (!hasEdits) return;
        const onClick = (e: MouseEvent) => {
            if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
            const link = (e.target as Element | null)?.closest?.('a[href]') as HTMLAnchorElement | null;
            if (!link || link.target === '_blank' || link.hasAttribute('download')) return;
            const url = new URL(link.href, window.location.href);
            if (url.origin !== window.location.origin || url.pathname === window.location.pathname) return;
            e.preventDefault();
            e.stopPropagation();
            setPendingLeave(() => () => router.push(url.pathname + url.search + url.hash));
        };
        document.addEventListener('click', onClick, true);
        return () => document.removeEventListener('click', onClick, true);
    }, [hasEdits, router]);

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

    // ⌘K's New workflow lands here as `?new=<n>`: the same as clicking New, once
    // per request. The request then leaves the address — the address effect above
    // does not run again when this page is already open, so it would linger.
    const newHandledRef = useRef<string | undefined>(undefined);
    const onNewRequested = useEffectEvent(() => guardLeave(createNew));
    useEffect(() => {
        if (!newRequest || newHandledRef.current === newRequest) return;
        newHandledRef.current = newRequest;
        onNewRequested();
        const url = new URL(window.location.href);
        if (url.searchParams.has('new')) {
            url.searchParams.delete('new');
            window.history.replaceState(null, '', `${url.pathname}${url.search}`);
        }
    }, [newRequest]);

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
            on_exhausted_step_id: remap(step.on_exhausted_step_id),
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

    // --- Duplicate a step: the selected one (Cmd+D), or the one the canvas's step toolbar is over ---
    const duplicateStep = useCallback((stepId: string) => {
        if (!draft) return;
        const step = draft.steps.find(s => s.id === stepId);
        if (!step) return;
        const copy = cloneStep(step);
        setDraft({ ...draft, steps: [...draft.steps, copy] });
        setSelectedStepId(copy.id);
    }, [draft, setDraft]);
    const duplicateSelectedStep = useCallback(() => {
        if (selectedStepId) duplicateStep(selectedStepId);
    }, [selectedStepId, duplicateStep]);

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
                    setRunStepErrors(prev => ({ ...prev, ...stepErrorsFrom(data.step_history) }));
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
                        setHumanContext(data.human_context ? { text: String(data.human_context), key: null } : null);
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
        askedStepRef.current = null;
        setRunStepErrors({});
        historyErrorsRef.current = {};
        setRunStatus('running');
        setRunLog([]);
        setHumanPrompt(null);
        setLiveActivity(null);
        lastEventIdRef.current = 0;  // fresh run, fresh journal
        setRunDrawer('run');

        streamSSE(`/api/orchestrations/${draft.id}/run`, { message: runInput });
    };

    // `fromJournal` marks events arriving from the replay/tail stream. A
    // journal can contain a terminal event mid-file (cancel, then resume
    // appends more events to the same run), so terminal handlers must NOT
    // abort the feed there — the server signals the real end with
    // `stream_complete`. Aborting on the live POST stream stays correct.
    const handleSSEEvent = (data: any, fromJournal = false) => {
        // A nested run's own step events arrive under the step that called it
        // (F251): they are logged with both names and never move that step's
        // card — a child's step ending used to read as the parent step's.
        const handleNestedStepEvent = (data: any) => {
            const where = `${data.step_name} › ${data.nested_step_name || 'a nested step'}`;
            const key = `${data.nested_run_id}:${data.nested_step_id || ''}`;
            switch (data.type) {
                case 'step_start':
                    setRunLog(prev => [...prev, line('start', `${where} (${data.step_type})`)]);
                    if (['agent', 'llm', 'print', 'extract_json'].includes(data.step_type)) {
                        pendingStepResultRef.current.set(key, { step_name: where, step_type: data.step_type, content: '' });
                    }
                    break;
                case 'step_complete': {
                    const pending = pendingStepResultRef.current.get(key);
                    setRunLog(prev => {
                        const next = [...prev, line('ok', `${where} completed (${data.duration_seconds?.toFixed(1)}s)`)];
                        if (pending && pending.content.trim()) {
                            next.splice(next.length - 1, 0, {
                                kind: 'step_result',
                                step_name: pending.step_name,
                                step_type: pending.step_type,
                                content: pending.content.trim(),
                            } as StepResultLogEntry);
                        }
                        return next;
                    });
                    pendingStepResultRef.current.delete(key);
                    break;
                }
                case 'step_error':
                    setRunLog(prev => [...prev, line('error', `${where}: ${failureReason(data, historyErrorsRef.current)}`)]);
                    pendingStepResultRef.current.delete(key);
                    break;
                case 'final':
                    if (data.intent !== 'orchestration' && data.response && pendingStepResultRef.current.has(key)) {
                        pendingStepResultRef.current.get(key)!.content = data.response;
                    }
                    break;
                default:
                    // The child's own task list and its own end: the parent's step
                    // reports how it went.
                    break;
            }
        };
        if (data.nested_run_id && NESTED_STEP_EVENTS.has(data.type)) {
            handleNestedStepEvent(data);
            return;
        }
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
                {
                    // Another step starting is the answer having been taken, so the
                    // step that asked is done: the engine sends no step_complete for
                    // it (F190). Live, on a replay, and after a refused answer alike.
                    const asked = askedStepRef.current;
                    if (asked && asked !== data.orch_step_id) askedStepRef.current = null;
                    setRunStepStatuses(prev => {
                        const next: Record<string, RunStepStatus> = { ...prev, [data.orch_step_id]: 'running' };
                        if (asked && asked !== data.orch_step_id && (prev[asked] === 'paused' || prev[asked] === 'running')) {
                            next[asked] = 'completed';
                        }
                        return next;
                    });
                }
                // A step run again (resume from failure) is not failing yet.
                setRunStepErrors(prev => {
                    if (!(data.orch_step_id in prev)) return prev;
                    const next = { ...prev };
                    delete next[data.orch_step_id];
                    return next;
                });
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

            case 'step_error': {
                // The event's `error` is a sanitised sentence; the reason is the
                // step's own words, which a member reading this log is owed (F195).
                const reason = failureReason(data, historyErrorsRef.current);
                setLiveActivity(null);
                setRunStepStatuses(prev => ({ ...prev, [data.orch_step_id]: 'failed' }));
                setRunStepErrors(prev => ({ ...prev, [data.orch_step_id]: reason }));
                setRunLog(prev => [...prev, line('error', `Step error: ${reason}`)]);
                pendingStepResultRef.current.delete(data.orch_step_id);
                break;
            }

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
                // An evaluator has already said where it went, in its own
                // `evaluator_decision` line: printing the router's too wrote
                // every decision twice (U66). The step's line is the one that
                // is always there — when its label is not in the route map the
                // router says nothing at all.
                if (st === 'evaluator') break;
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
                if (data.orch_step_id) setRunStepStatuses(prev => ({ ...prev, [data.orch_step_id]: 'completed' }));
                setRunLog(prev => [...prev, line('end', 'End node reached')]);
                break;

            case 'human_input_required':
                setLiveActivity(null);
                setRunStatus('paused');
                if (data.orch_step_id) {
                    askedStepRef.current = data.orch_step_id;
                    setRunStepStatuses(prev => ({ ...prev, [data.orch_step_id]: 'paused' }));
                }
                setHumanPrompt(data.prompt || 'Please provide input:');
                setHumanContext(data.agent_context
                    ? { text: String(data.agent_context), key: data.context_key || null }
                    : null);
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
                setRunStepStatuses(prev => settleRun(prev, data.status === 'completed' ? 'completed' : 'failed'));
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
                if (isCancel(data)) {
                    // Not a failure. The tab that cancelled has said so already.
                    setRunStatus('cancelled');
                    setRunStepStatuses(prev => settleRun(prev, 'cancelled'));
                    setRunLog(prev => prev[prev.length - 1] === CANCELLED_LINE ? prev : [...prev, CANCELLED_LINE]);
                } else {
                    setRunStatus('failed');
                    setRunStepStatuses(prev => settleRun(prev, 'failed'));
                    setRunLog(prev => [...prev, line('error', `Error: ${data.error}`)]);
                }
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

            case 'knowledge_retrieve':
                setRunLog(prev => [...prev, {
                    kind: 'retrieval',
                    step_name: data.step_name,
                    query_id: data.query_id,
                    hit_count: data.hit_count ?? 0,
                    cost_usd: data.cost_usd ?? 0,
                    degraded: Array.isArray(data.degraded) ? data.degraded : [],
                } as RetrievalLogEntry]);
                break;

            case 'knowledge_ingest': {
                const docs: { status?: string }[] = Array.isArray(data.documents) ? data.documents : [];
                const byStatus = new Map<string, number>();
                for (const d of docs) byStatus.set(d.status || 'unknown', (byStatus.get(d.status || 'unknown') || 0) + 1);
                const statuses = [...byStatus].map(([status, n]) => `${n} ${status}`).join(', ');
                setRunLog(prev => [...prev, line('info',
                    `Ingest · ${data.step_name || ''}: ${docs.length} record${docs.length !== 1 ? 's' : ''} → ${data.kb_id || ''}${statuses ? ` · ${statuses}` : ''}`)]);
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
        setRunStepStatuses(prev => settleRun(prev, 'cancelled'));
        setRunLog(prev => [...prev, CANCELLED_LINE]);
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
        setRunDrawer('run');
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

    const allStepIds = draft?.steps.map(s => ({ id: s.id, name: s.name })) || [];


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

    // The address names what is open, so a reload or a shared link reopens it:
    // `?id=` follows the orchestration on screen, and the list is bare
    // `/orchestrations`. Duplicate opened the copy under the original's `?id=`,
    // and a reload went back to the original (F198). A `?run=` link stays only
    // while the orchestration it opened is still the one shown. Written with
    // `history.replaceState`, which Next keeps in step, so nothing re-renders.
    const urlOrchRef = useRef<string | null>(null);
    useEffect(() => {
        if (initialRunId && !initialRunConsumedRef.current) return;  // the run link is still to open
        const url = new URL(window.location.href);
        const run = url.searchParams.get('run');
        const next = new URLSearchParams();
        if (realOrchId) {
            next.set('id', realOrchId);
            if (run && urlOrchRef.current === null) next.set('run', run);
        }
        urlOrchRef.current = realOrchId;
        const search = next.toString();
        if (search === url.searchParams.toString()) return;
        window.history.replaceState(null, '', `${url.pathname}${search ? `?${search}` : ''}`);
    }, [realOrchId, initialRunId]);

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
            {/* The list's toolbar: the picker, New and Build with AI. One row from `sm`
                up (F218). The open canvas has its own floating bar (M3a). */}
            {!draft && (
            <div className="flex flex-wrap items-center gap-x-3 gap-y-2 px-4 py-2.5 border-b border-zinc-800 shrink-0">
                <div className="flex items-center gap-2 w-full min-w-0 sm:w-auto sm:flex-1">
                    <Combobox
                        value={selectedOrchId || undefined}
                        onChange={(id: string) => selectOrchestration(id || null)}
                        placeholder="Select orchestration…"
                        searchPlaceholder="Search orchestrations…"
                        aria-label="Orchestration"
                        size="sm"
                        className="max-w-[240px] min-w-0"
                        options={orchestrations.map(o => ({ value: o.id, label: o.name }))}
                    />
                    <button
                        onClick={createNew}
                        aria-label="New orchestration"
                        className="flex shrink-0 items-center gap-1 px-2 sm:px-3 py-1.5 text-xs bg-blue-600 hover:bg-blue-500 text-white transition-colors"
                    >
                        <Plus size={14} aria-hidden /> <span className="hidden sm:inline">New</span>
                    </button>
                    <button
                        onClick={() => openBuilder()}
                        aria-label="Build with AI"
                        className="flex shrink-0 items-center gap-1.5 px-2 sm:px-3 py-1.5 text-xs whitespace-nowrap bg-accent hover:bg-accent-hover text-accent-fg transition-colors"
                    >
                        <Mark size={20} compact={false} className="-my-0.5" /> <span className="hidden sm:inline">Build with AI</span>
                    </button>
                </div>
            </div>
            )}

            {/* Active runs banner (the list; the open canvas's run strip has them as a menu) */}
            {!draft && activeRuns.filter(r => r.run_id !== runId).length > 0 && (
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
                                                            <button
                                                                type="button"
                                                                onClick={(e) => { e.stopPropagation(); selectOrchestration(o.id); }}
                                                                aria-label={`Open ${o.name}`}
                                                                className="text-[11px] text-text-faint group-hover:text-zinc-300 hover:text-zinc-100 transition-colors rounded-sm"
                                                            >
                                                                Open →
                                                            </button>
                                                        </td>
                                                    </tr>
                                                );
                                            })}
                                        </tbody>
                                    </table>
                                </div>
                            </div>
                        )}

                        {landingTab === 'active' && (
                            <RunList
                                rows={activeRuns.map(r => ossRunRow(r, orchestrations, schedules))}
                                workflows={orchestrations}
                                filters={false}
                                search={false}
                                onOpen={openLandingRun}
                                empty={<p className="px-2.5 py-10 text-center text-xs italic text-text-faint">No active runs — start one from the Orchestrations tab.</p>}
                            />
                        )}

                        {landingTab === 'recent' && (
                            <RunList
                                rows={pastRuns.filter(r => !isLiveRun(r)).slice(0, 10).map(r => ossRunRow(r, orchestrations, schedules))}
                                workflows={orchestrations}
                                filters={false}
                                search={false}
                                onOpen={openLandingRun}
                                empty={<p className="px-2.5 py-10 text-center text-xs italic text-text-faint">No finished runs yet.</p>}
                            />
                        )}

                        {landingTab === 'all' && (
                            <RunList
                                rows={(allRuns.length > 0 ? allRuns : pastRuns).map(r => ossRunRow(r, orchestrations, schedules))}
                                workflows={orchestrations}
                                filters={false}
                                search={false}
                                onOpen={openLandingRun}
                                empty={<p className="px-2.5 py-10 text-center text-xs italic text-text-faint">No runs recorded yet.</p>}
                            />
                        )}
                    </div>
                </div>
            ) : (
                // The open canvas (Prototype 4, D204): the workflow fills the sheet and
                // everything else floats over it. Every control of the old editor is
                // here, moved (M3a's field map): the toolbar's into the bar and the
                // dock, the panels' into the card's Workflow settings, the run log's
                // into the run strip and drawer.
                <div className="relative min-h-0 flex-1">
                    <CanvasWorkbench
                        orchestration={draft}
                        onChange={updateOrchestration}
                        agents={agents}
                        knowledgeEnabled={knowledgeEnabled}
                        selectedStepId={selectedStepId}
                        onSelectStep={setSelectedStepId}
                        renderStep={(step) => (
                            <StepConfigPanel
                                frame="card"
                                step={step}
                                agents={agents}
                                allStepIds={allStepIds}
                                onUpdate={updateStep}
                                onDelete={() => deleteStep(step.id)}
                                onClose={() => setSelectedStepId(null)}
                                isEntry={draft.entry_step_id === step.id}
                                onSetEntry={() => setEntryPoint(step.id)}
                                availableModels={availableModels}
                                orchestration={draft}
                                knowledgeEnabled={knowledgeEnabled}
                                knowledgeBases={knowledgeBases}
                                semanticCacheNote={semanticCacheNote}
                            />
                        )}
                        renderSettings={(close) => (
                            <WorkflowSettingsPanel name={draft.name} onClose={close}>
                                <SettingsSection title="Description">
                                    <DescriptionField draft={draft} onChange={setDraft} />
                                </SettingsSection>
                                <SettingsSection title="Schedules">
                                    <SchedulesList schedules={schedules} workflowId={draft.id} href="/schedules" />
                                </SettingsSection>
                                {/* Its own heading and Add key, as in the State Schema tab. */}
                                <StateSchemaEditor
                                    schema={draft.state_schema}
                                    onChange={(schema) => setDraft({ ...draft, state_schema: schema })}
                                />
                                <SettingsSection title="Guardrails">
                                    <GuardrailsFields draft={draft} onChange={setDraft} rubrics={rubrics} />
                                </SettingsSection>
                            </WorkflowSettingsPanel>
                        )}
                        stepActions={{
                            duplicate: duplicateStep,
                            setEntry: setEntryPoint,
                            remove: deleteStep,
                        }}
                        history={{ undo, redo, canUndo, canRedo }}
                        save={{ dirty, saving, onSave: handleSave }}
                        onBack={() => guardLeave(backToList)}
                        switcher={
                            <Combobox
                                trigger="icon"
                                value={selectedOrchId || undefined}
                                onChange={(id: string) => guardLeave(() => selectOrchestration(id || null))}
                                placeholder="Select orchestration…"
                                searchPlaceholder="Search orchestrations…"
                                aria-label="Orchestration"
                                size="sm"
                                options={orchestrations.map(o => ({ value: o.id, label: o.name }))}
                            />
                        }
                        menu={[
                            { id: 'new', label: 'New orchestration', icon: Plus, onSelect: () => guardLeave(createNew), separatorBefore: true },
                            { id: 'deploy', label: 'Deploy as Agent', icon: Bot, onSelect: handleDeploy },
                            { id: 'duplicate', label: 'Duplicate', icon: Copy, onSelect: handleDuplicate, disabled: saving },
                            { id: 'delete', label: 'Delete', icon: Trash, onSelect: handleDelete, danger: true, separatorBefore: true },
                        ]}
                        onChangeWithAI={() => openBuilder({ currentOrchestrationId: realOrchId })}
                        insetRight={builderInset}
                        run={{
                            phase: runStatus,
                            stepStatuses: runStepStatuses,
                            stepErrors: runStepErrors,
                            onRun: startRun,
                            onStop: cancelRun,
                            input: { value: runInput, onChange: setRunInput },
                        }}
                        runStrip={
                            <RunStrip
                                runStatus={runStatus}
                                liveActivity={liveActivity}
                                runId={runId}
                                humanPrompt={humanPrompt}
                                humanContext={humanContext}
                                humanResponse={humanResponse}
                                setHumanResponse={setHumanResponse}
                                humanFields={humanFields}
                                humanValues={humanValues}
                                setHumanValues={setHumanValues}
                                onSubmitHuman={submitHumanInput}
                                onResumeRun={resumeRun}
                                drawer={runDrawer}
                                onDrawer={setRunDrawer}
                                pastRunCount={pastRuns.length}
                                activeRuns={activeRuns.filter(r => r.run_id !== runId)}
                                workflowName={(id) => orchestrations.find(o => o.id === id)?.name ?? id}
                                onRestoreRun={(run) => guardLeave(() => restoreRun(run))}
                            />
                        }
                        runDrawer={runDrawer ? {
                            open: true,
                            content: (
                                <RunDrawer
                                    view={runDrawer}
                                    onClose={() => setRunDrawer(null)}
                                    runStatus={runStatus}
                                    liveActivity={liveActivity}
                                    runLog={runLog}
                                    onOpenResponseModal={setResponseModal}
                                    runId={runId}
                                    humanPrompt={humanPrompt}
                                    orchestrationNames={Object.fromEntries(orchestrations.map(o => [o.id, o.name]))}
                                    pastRuns={pastRuns}
                                    onRestoreRun={(run) => guardLeave(() => restoreRun(run))}
                                />
                            ),
                        } : undefined}
                    />

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

            <ConfirmDialog
                open={pendingLeave !== null}
                onClose={() => setPendingLeave(null)}
                onConfirm={() => {
                    const go = pendingLeave;
                    setPendingLeave(null);
                    go?.();
                }}
                title="Discard unsaved changes?"
                description={<>Your changes to <strong>{draft?.name || 'this workflow'}</strong> have not been saved.</>}
                confirmLabel="Discard changes"
            />

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
