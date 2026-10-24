'use client';

/**
 * Workflows as tabs (TABS1 v2, D319; F930, F1022, F1025).
 *
 * Several workflows open at once, and a run beside its workflow. The page
 * mounts one editor, keyed by the tab in front, so only that tab has a canvas
 * and reads its run; a tab going behind keeps its draft here in memory (never
 * stored) and the run it follows, and comes forward from both. The tabs behind
 * learn whether their run is going or waiting on a person from one short read.
 *
 * A link inside the page to a run or a workflow — a step's "Open the run", the
 * run strip, a run's "Open in editor" — opens it as a tab instead of leaving.
 * Only clicks inside this page are taken: the menu, the help panel and dialogs
 * (which portal out of it) navigate as they always did, and a modified click
 * still opens the browser's own tab.
 */

import { Plus, X } from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { RunPage } from '@/components/runs/RunPage';
import { OrchestrationTab, type EditorTabs } from '@/components/settings/OrchestrationTab';
import {
    NewTabMenu, TabStrip, TabSwitcher, restoreWorkflowTabs, useTabShortcuts, useWorkflowTabs,
    type StripTab, type TabStatus, type TabsState, type WorkflowTab,
} from '@/components/tabs';
import { ConfirmDialog, IconButton } from '@/components/ui';
import type { DraftSnapshot } from '@/components/orchestration/use-draft-history';

/** Where this browser keeps the open workflow tabs. Drafts are never kept: they live in the page. */
const TABS_KEY = 'synapseTabs:workflows';

function loadTabs(): Promise<TabsState<WorkflowTab> | null> {
    try {
        const raw = JSON.parse(localStorage.getItem(TABS_KEY) || 'null');
        return Promise.resolve(raw ? restoreWorkflowTabs(raw) : null);
    } catch {
        return Promise.resolve(null);
    }
}

function saveTabs(state: TabsState<WorkflowTab>) {
    try {
        localStorage.setItem(TABS_KEY, JSON.stringify({
            tabs: state.tabs.map(({ id, kind, ref, key, title, usedAt, workflowId }) => ({
                id, kind, ref, key, title, usedAt, ...(workflowId ? { workflowId } : {}),
            })),
            activeId: state.activeId,
        }));
    } catch { /* a full or blocked storage: the tabs last this page's life */ }
}

/** One read for every run the tabs follow (`POST /api/orchestrations/runs/status`). */
async function readRuns(runs: { tabId: string; runId: string }[]): Promise<Record<string, TabStatus>> {
    if (runs.length === 0) return {};
    const res = await fetch('/api/orchestrations/runs/status', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ run_ids: [...new Set(runs.map(r => r.runId))] }),
    });
    if (!res.ok) return {};
    const rows: { run_id: string; status: string; waiting_for_human: boolean }[] = (await res.json()).runs ?? [];
    const byId = new Map(rows.map(r => [r.run_id, r]));
    const out: Record<string, TabStatus> = {};
    for (const { tabId, runId } of runs) {
        const r = byId.get(runId);
        if (r) out[tabId] = {
            running: r.status === 'running' || r.status === 'paused',
            pendingApproval: !!r.waiting_for_human,
            turnCount: 0,
            lastUpdated: null,
        };
    }
    return out;
}

const newWorkflowId = () => 'orch_' + Math.random().toString(36).substring(2, 9);

/** What a link inside the page names, when it names a run or a workflow. */
function tabLink(url: URL): { run?: string; workflow?: string } | null {
    if (url.origin !== window.location.origin) return null;
    const run = /^\/runs\/([^/]+)\/?$/.exec(url.pathname);
    if (run) return { run: decodeURIComponent(run[1]) };
    if (url.pathname === '/workflows' || url.pathname === '/orchestrations') {
        const id = url.searchParams.get('id');
        const runId = url.searchParams.get('run');
        if (runId) return { run: runId, workflow: id ?? undefined };
        if (id) return { workflow: id };
    }
    return null;
}

const onPhone = () => window.matchMedia('(max-width: 767px)').matches;

type RecentRun = { run_id: string; orchestration_id: string; status: string; waiting_for_human?: boolean;
                   started_at?: string | null; ended_at?: string | null };

/** The new tab's recent runs: live ones first, then the newest, ten at most. */
function recentRuns(runs: RecentRun[]): RecentRun[] {
    const live = (r: RecentRun) => r.status === 'running' || r.status === 'paused';
    return [...runs.filter(live), ...runs.filter(r => !live(r))].slice(0, 10);
}

/** A recent run in a few words: how it stands, and when. */
function runWords(r: RecentRun): string {
    const state = r.waiting_for_human ? 'Waiting on you'
        : r.status === 'running' ? 'Running'
        : r.status === 'paused' ? 'Paused'
        : r.status === 'completed' ? 'Completed'
        : r.status === 'failed' ? 'Failed'
        : r.status === 'cancelled' ? 'Cancelled' : r.status;
    const at = r.ended_at || r.started_at;
    if (!at) return state;
    const mins = Math.max(0, Math.round((Date.now() - new Date(at).getTime()) / 60000));
    const ago = mins < 1 ? 'just now' : mins < 60 ? `${mins} min ago` : mins < 1440 ? `${Math.round(mins / 60)} h ago` : `${Math.round(mins / 1440)} d ago`;
    return `${state} · ${ago}`;
}

export function WorkflowTabs({ initialRunId, initialOrchId, newRequest, openSettings }: {
    initialRunId?: string;
    initialOrchId?: string;
    newRequest?: string;
    openSettings?: boolean;
}) {
    const tabs = useWorkflowTabs<DraftSnapshot>({ load: loadTabs, save: saveTabs, readStatus: readRuns, newWorkflowId });
    const { ready, start, state, front, notice, setNotice, badges, unsaved, kept } = tabs;
    const [workflows, setWorkflows] = useState<{ id: string; name: string; description?: string }[]>([]);
    const [recent, setRecent] = useState<RecentRun[]>([]);
    const [picker, setPicker] = useState<'strip' | 'phone' | null>(null);
    const [closing, setClosing] = useState<WorkflowTab | null>(null);
    const shellRef = useRef<HTMLDivElement>(null);

    const readWorkflows = useCallback(() => {
        fetch('/api/orchestrations').then(r => (r.ok ? r.json() : [])).then(list => {
            if (Array.isArray(list)) setWorkflows(list.map((o: { id: string; name: string; description?: string }) =>
                ({ id: o.id, name: o.name, description: o.description })));
        }).catch(() => {});
    }, []);
    useEffect(() => { readWorkflows(); }, [readWorkflows]);
    // The picker's recent runs (Naveen): read when it opens.
    const readRecent = useCallback(() => {
        fetch('/api/orchestrations/runs?limit=30').then(r => (r.ok ? r.json() : [])).then(list => {
            if (Array.isArray(list)) setRecent(list);
        }).catch(() => {});
    }, []);
    const nameOf = useCallback((id?: string) => workflows.find(w => w.id === id)?.name ?? '', [workflows]);

    /** Open a run as a tab: beside its workflow, named after it. */
    const openRun = useCallback(async (runId: string, workflowId?: string) => {
        let wf = workflowId;
        if (!wf) {
            try {
                const res = await fetch(`/api/orchestrations/runs/${encodeURIComponent(runId)}`);
                if (res.ok) wf = (await res.json()).orchestration_id || undefined;
            } catch { /* opened without its workflow's name */ }
        }
        tabs.openRun(runId, wf, `Run · ${nameOf(wf) || 'workflow'}`);
    }, [tabs, nameOf]);

    // The tabs this browser kept, then what the address asks for: a workflow, a run on its canvas, or a new one.
    useEffect(() => {
        void start(saved => saved);
        // Once.
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);
    // Each ask once: on arrival, and again when ⌘K or a link changes the address while this page is open — the
    // page renders again with the new address, and this shell stays mounted.
    const asked = useRef<string | null>(null);
    useEffect(() => {
        const ask = `${initialOrchId ?? ''}|${initialRunId ?? ''}|${newRequest ?? ''}`;
        if (!ready || asked.current === ask) return;
        asked.current = ask;
        const id = initialOrchId, run = initialRunId, wantsNew = newRequest;
        if (wantsNew) tabs.openNew();
        else if (run) {
            void (async () => {
                try {
                    const res = await fetch(`/api/orchestrations/runs/${encodeURIComponent(run)}`);
                    const wf = res.ok ? (await res.json()).orchestration_id : null;
                    if (wf) tabs.openWorkflow(wf, run);
                } catch { /* the list, as before */ }
            })();
        } else if (id) tabs.openWorkflow(id);
        if (wantsNew) {
            const url = new URL(window.location.href);
            url.searchParams.delete('new');
            window.history.replaceState(null, '', `${url.pathname}${url.search}`);
        }
    }, [ready, tabs, initialOrchId, initialRunId, newRequest]);

    // A link inside the page to a run or a workflow opens a tab (F1025). Listened for on this page's own
    // element: the menu and the help panel are outside it, and dialogs portal out of it.
    useEffect(() => {
        const shell = shellRef.current;
        if (!shell) return;
        const onClick = (e: MouseEvent) => {
            if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
            const link = (e.target as Element | null)?.closest?.('a[href]') as HTMLAnchorElement | null;
            if (!link || link.target === '_blank' || link.hasAttribute('download')) return;
            const named = tabLink(new URL(link.href, window.location.href));
            if (!named) return;
            e.preventDefault();
            e.stopPropagation();
            if (named.workflow && named.run) tabs.openWorkflow(named.workflow, named.run);
            else if (named.run) void openRun(named.run);
            else if (named.workflow) tabs.openWorkflow(named.workflow);
        };
        shell.addEventListener('click', onClick, true);
        return () => shell.removeEventListener('click', onClick, true);
    }, [tabs, openRun]);

    // Unsaved work in a tab behind is guarded as the tab in front's is.
    const behindDirty = state.tabs.some(t => t.id !== state.activeId && kept.get(t.id)?.dirty);
    useEffect(() => {
        if (!behindDirty) return;
        const handler = (e: BeforeUnloadEvent) => { e.preventDefault(); };
        window.addEventListener('beforeunload', handler);
        return () => window.removeEventListener('beforeunload', handler);
    }, [behindDirty]);

    /** Close a tab; one holding unsaved work asks first. */
    const requestClose = useCallback((id: string) => {
        const tab = state.tabs.find(t => t.id === id);
        if (!tab) return;
        if (unsaved[id]) setClosing(tab);
        else tabs.close(id);
    }, [state.tabs, unsaved, tabs]);

    useTabShortcuts((key) => {
        if (key.kind === 'new') setPicker(onPhone() ? 'phone' : 'strip');
        else if (key.kind === 'close') { if (state.activeId) requestClose(state.activeId); }
        else if (state.tabs[key.index]) tabs.focus(state.tabs[key.index].id);
    }, ready);

    const stripTabs = useMemo<TabsState<WorkflowTab & StripTab>>(() => ({
        tabs: state.tabs.map(t => ({
            ...t,
            title: t.title || (t.kind === 'workflow' ? nameOf(t.ref) : `Run · ${nameOf(t.workflowId)}`),
            tileName: t.kind === 'run' ? 'Run' : (t.title || nameOf(t.ref) || 'Workflow'),
            tileSeed: t.kind === 'run' ? t.workflowId ?? t.ref : t.ref,
        })),
        activeId: state.activeId,
    }), [state, nameOf]);

    const picked = workflows.map(w => ({ id: w.id, name: w.name, note: w.description || null }));
    const runItems = recentRuns(recent).map(r => ({
        id: r.run_id,
        name: nameOf(r.orchestration_id) || 'A workflow since deleted',
        note: runWords(r),
    }));
    const pickerProps = {
        agents: picked,
        onPick: (id: string) => tabs.openWorkflow(id, undefined, nameOf(id)),
        label: 'Open a workflow in a new tab',
        heading: 'Workflows',
        listLabel: 'Open a workflow',
        placeholder: 'Search workflows…',
        footer: 'Opens beside your other tabs. Nothing runs until you press Run.',
        first: { label: 'New workflow', icon: <Plus className="size-4" aria-hidden />, onPick: tabs.openNew },
        more: {
            heading: 'Recent runs',
            items: runItems,
            onPick: (runId: string) => {
                const run = recent.find(r => r.run_id === runId);
                void openRun(runId, run?.orchestration_id || undefined);
            },
        },
    };

    const strip = ready && state.tabs.length > 0 ? (
        <>
            <div data-tour="workflow-tabs" className="hidden shrink-0 items-end gap-2 border-b border-border bg-bg px-3 pt-2 md:flex">
                <TabStrip
                    label="Open workflows"
                    state={stripTabs}
                    badges={badges}
                    unsaved={unsaved}
                    onFocus={tabs.focus}
                    onClose={requestClose}
                    onMove={tabs.move}
                    untitled="New workflow"
                    newTab={(
                        <NewTabMenu
                            {...pickerProps}
                            open={picker === 'strip'}
                            onOpenChange={o => { setPicker(o ? 'strip' : null); if (o) { readWorkflows(); readRecent(); } }}
                        />
                    )}
                />
            </div>
            <div className="flex shrink-0 items-center gap-1 border-b border-border bg-bg px-3 py-2 md:hidden">
                <TabSwitcher
                    state={stripTabs}
                    badges={badges}
                    unsaved={unsaved}
                    onFocus={tabs.focus}
                    onClose={requestClose}
                    onNew={() => setPicker('phone')}
                    untitled="New workflow"
                    subtitle={t => (t.kind === 'run' ? 'Run' : 'Workflow')}
                    className="min-w-0 flex-1"
                />
                <NewTabMenu
                    {...pickerProps}
                    open={picker === 'phone'}
                    onOpenChange={o => { setPicker(o ? 'phone' : null); if (o) { readWorkflows(); readRecent(); } }}
                    className="size-11"
                />
            </div>
            {notice && (
                <div role="status" className="flex shrink-0 items-center gap-3 border-b border-border bg-surface px-4 py-2 text-xs text-text-muted">
                    <span className="min-w-0 flex-1">{notice}</span>
                    <IconButton icon={X} label="Dismiss" size="sm" onClick={() => setNotice(null)} />
                </div>
            )}
        </>
    ) : null;

    const frontKept = front ? kept.get(front.id) : undefined;
    const bridge: EditorTabs = {
        startNew: !!front && tabs.isFresh(front.id),
        history: frontKept?.history,
        viewport: frontKept?.viewport,
        open: (workflowId, runId) => tabs.openWorkflow(workflowId, runId, nameOf(workflowId)),
        openNew: tabs.openNew,
        showList: () => tabs.focus(null),
        keep: ({ history, runId, dirty, viewport }) => {
            if (front) tabs.keep(front.id, { history, runId, dirty, viewport });
        },
        report: (now) => {
            tabs.setFrontReport(now);
            if (front && now.title && now.title !== front.title) tabs.change(front.id, { title: now.title });
        },
        gone: () => { if (front) tabs.close(front.id); },
        isTabLink: (url) => tabLink(url) !== null,
        othersDirty: behindDirty,
        strip,
    };

    return (
        <div ref={shellRef} className="h-full min-h-0">
            {front?.kind === 'run' ? (
                <div className="flex h-full min-h-0 flex-col">
                    {strip}
                    <div className="min-h-0 flex-1 overflow-y-auto">
                        <RunPage key={front.id} runId={front.ref} />
                    </div>
                </div>
            ) : (
                <OrchestrationTab
                    key={front?.id ?? 'list'}
                    initialOrchId={front?.ref}
                    initialRunId={front ? frontKept?.runId ?? undefined : undefined}
                    openSettings={!!openSettings && front?.ref === initialOrchId}
                    tabs={bridge}
                />
            )}
            <ConfirmDialog
                open={closing !== null}
                onClose={() => setClosing(null)}
                onConfirm={() => {
                    if (closing) tabs.close(closing.id);
                    setClosing(null);
                }}
                title="Discard unsaved changes?"
                description={<>Your changes to <strong>{closing?.title || nameOf(closing?.ref) || 'this workflow'}</strong> have not been saved.</>}
                confirmLabel="Discard changes"
            />
        </div>
    );
}
