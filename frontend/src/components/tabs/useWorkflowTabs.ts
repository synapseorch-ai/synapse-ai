"use client";

/*
 * The workflows page's tabs, as one hook both products share (TABS1 v2, F930/F1022).
 *
 * A tab is a workflow being edited or one of its runs. Only the tab in front
 * is mounted; a tab going behind leaves its draft and the run it follows here
 * (`kept`), and comes forward from them. Unlike chat, nothing is ever closed for
 * the person: a tab may hold unsaved work, so at the cap a new tab waits while
 * they pick one to close (F1354: no tab is in front meanwhile, so the page never
 * shows a workflow other than the one asked for), and a tab with unsaved work
 * closes only after they say so.
 */

import * as React from "react";

import {
  activeTab,
  closeTab,
  focusTab,
  moveTab,
  openTab,
  updateTab,
  workflowKey,
  type TabBadge,
  type TabStatus,
  type TabsState,
  type WorkflowTab,
} from "./model";
import { useTabStatus } from "./useTabStatus";

/** What a tab behind left with: its draft (opaque here) and the run it follows. */
export interface KeptTab<D> {
  history?: D;
  /** Where its canvas stood: it comes back there (Naveen). */
  viewport?: { x: number; y: number; zoom: number } | null;
  runId?: string | null;
  /** Its draft holds work that isn't saved. */
  dirty?: boolean;
}

/** At the cap: the tab asked for, waiting for one to close, and the tab that was in front. */
export interface AskAtCap {
  tab: WorkflowTab;
  from: string | null;
  besideId?: string | null;
}

/** What the tab in front says of itself. */
export interface FrontReport {
  title: string;
  dirty: boolean;
  running: boolean;
  waiting: boolean;
  workflowId: string | null;
}

const SAVE_AFTER_MS = 400;

export function useWorkflowTabs<D>({
  load,
  save,
  readStatus,
  newWorkflowId,
}: {
  load: () => Promise<TabsState<WorkflowTab> | null>;
  save: (state: TabsState<WorkflowTab>) => void;
  /** One read for the runs the tabs follow, as TabStatus by tab id (running; waiting = pendingApproval). */
  readStatus: (runs: { tabId: string; runId: string }[]) => Promise<Record<string, TabStatus>>;
  newWorkflowId: () => string;
}) {
  const [ready, setReady] = React.useState(false);
  const [state, setState] = React.useState<TabsState<WorkflowTab>>({ tabs: [], activeId: null });
  const [notice, setNotice] = React.useState<string | null>(null);
  const [asking, setAsking] = React.useState<AskAtCap | null>(null);
  const [front, setFront] = React.useState<FrontReport | null>(null);
  // Kept per tab, in this page's memory only: a draft is never stored.
  const kept = React.useRef(new Map<string, KeptTab<D>>());
  const [keptVersion, bump] = React.useReducer((n: number) => n + 1, 0);
  const stateRef = React.useRef(state);
  React.useEffect(() => { stateRef.current = state; }, [state]);
  // Tabs opened just now as a new workflow: their editor starts blank.
  const fresh = React.useRef(new Set<string>());

  const adapters = React.useRef({ load, save, newWorkflowId });
  React.useEffect(() => { adapters.current = { load, save, newWorkflowId }; });

  const start = React.useCallback(async (seed: (kept: TabsState<WorkflowTab>) => TabsState<WorkflowTab>) => {
    let saved: TabsState<WorkflowTab> | null = null;
    try { saved = await adapters.current.load(); } catch { /* nothing kept is a fresh start */ }
    setState(seed(saved ?? { tabs: [], activeId: null }));
    setReady(true);
  }, []);

  React.useEffect(() => {
    if (!ready) return;
    const wait = setTimeout(() => adapters.current.save(state), SAVE_AFTER_MS);
    return () => clearTimeout(wait);
  }, [ready, state]);

  const make = React.useCallback((kind: WorkflowTab["kind"], ref: string, title = "", workflowId?: string): WorkflowTab => ({
    id: `wt_${Math.random().toString(36).slice(2, 10)}${Date.now().toString(36)}`,
    kind, ref, key: workflowKey(kind, ref), title, usedAt: Date.now(),
    ...(workflowId ? { workflowId } : {}),
  }), []);

  /** Open a tab, or bring forward the one already showing it. At the cap, nothing closes: the person is asked which
   *  tab to close, and none is in front until they answer (F1354). */
  const open = React.useCallback((tab: WorkflowTab, besideId?: string | null) => {
    setState((s) => {
      const result = openTab(s, tab, () => true, "ask");
      if (result.ask) {
        const ask = { tab: result.ask, from: s.activeId, besideId };
        queueMicrotask(() => setAsking(ask));
        return result.state;
      }
      if (result.refused) {
        queueMicrotask(() => setNotice(result.refused));
        return s;
      }
      // A run opens beside its workflow's tab.
      const at = besideId ? result.state.tabs.findIndex((t) => t.id === besideId) : -1;
      return at >= 0 && result.state.tabs.some((t) => t.id === tab.id)
        ? moveTab(result.state, tab.id, at + 1)
        : result.state;
    });
    setFront(null);
  }, []);

  const openWorkflow = React.useCallback((workflowId: string, runId?: string, title = "") => {
    const tab = make("workflow", workflowId, title);
    // The run to attach: to the tab already open on this workflow, or to the new one.
    const existing = stateRef.current.tabs.find((t) => t.key === tab.key);
    if (runId) kept.current.set(existing?.id ?? tab.id, { ...kept.current.get(existing?.id ?? tab.id), runId });
    open(tab);
  }, [make, open]);

  const openRun = React.useCallback((runId: string, workflowId?: string, title = "") => {
    const s = stateRef.current;
    const beside = (workflowId && s.tabs.find((t) => t.kind === "workflow" && t.ref === workflowId)?.id) || s.activeId;
    open(make("run", runId, title, workflowId), beside);
  }, [make, open]);

  const openNew = React.useCallback(() => {
    const tab = make("workflow", adapters.current.newWorkflowId(), "New workflow");
    fresh.current.add(tab.id);
    open(tab);
  }, [make, open]);

  const focus = React.useCallback((id: string | null) => {
    setNotice(null);
    setFront(null);
    setState((s) => (id === null ? { ...s, activeId: null } : s.activeId === id ? s : focusTab(s, id, Date.now())));
  }, []);

  /** Close without asking: the caller has asked when the tab held unsaved work. */
  const close = React.useCallback((id: string) => {
    setNotice(null);
    kept.current.delete(id);
    fresh.current.delete(id);
    setState((s) => closeTab(s, id, Date.now()));
    setFront(null);
  }, []);

  /** At the cap: close the tab they picked (the caller asked first when it held unsaved work), then open the one asked
   *  for, in front. */
  const chooseClose = React.useCallback((id: string) => {
    const ask = asking;
    setAsking(null);
    if (!ask) return;
    kept.current.delete(id);
    fresh.current.delete(id);
    setState((s) => closeTab(s, id, Date.now()));
    open(ask.tab, ask.besideId);
  }, [asking, open]);

  /** At the cap, they picked none: the tab that was in front comes back, and nothing new opens. */
  const cancelAsk = React.useCallback(() => {
    const ask = asking;
    setAsking(null);
    if (ask) {
      // The tab asked for never opened: what was kept for it (a run to follow, a blank start) goes with it.
      kept.current.delete(ask.tab.id);
      fresh.current.delete(ask.tab.id);
    }
    if (ask?.from) setState((s) => focusTab(s, ask.from!, Date.now()));
    setFront(null);
  }, [asking]);

  const move = React.useCallback((id: string, to: number) => setState((s) => moveTab(s, id, to)), []);

  /** A tab's title or workflow changed (a new workflow saved, a rename). */
  const change = React.useCallback((id: string, edit: Partial<WorkflowTab>) => {
    setState((s) => {
      const tab = s.tabs.find((t) => t.id === id);
      if (!tab) return s;
      const ref = edit.ref ?? tab.ref;
      return updateTab(s, id, { ...edit, key: workflowKey(edit.kind ?? tab.kind, ref) });
    });
  }, []);

  const keep = React.useCallback((id: string, left: KeptTab<D>) => {
    kept.current.set(id, { ...kept.current.get(id), ...left });
    fresh.current.delete(id);
    bump();
  }, []);

  // The runs the tabs behind follow, read together.
  const following = state.tabs
    .map((t) => ({ tabId: t.id, runId: t.kind === "run" ? t.ref : kept.current.get(t.id)?.runId ?? null }))
    .filter((r): r is { tabId: string; runId: string } => !!r.runId);
  const followingKey = following.map((r) => `${r.tabId}:${r.runId}`).join("|");
  const statusTabs = React.useMemo(
    () => following.map((r) => ({ id: r.tabId, key: r.runId, title: "", usedAt: 0 })),
    // `following` is rebuilt each render; its key says when it changed.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [followingKey],
  );
  const readFollowing = React.useCallback(
    (tabs: { id: string; key: string }[]) => readStatus(tabs.map((t) => ({ tabId: t.id, runId: t.key }))),
    [readStatus],
  );
  const { status } = useTabStatus({
    tabs: statusTabs,
    activeId: state.activeId,
    read: readFollowing,
    onMark: (id, mark) => setState((s) => updateTab(s, id, { mark } as Partial<WorkflowTab>)),
  });

  const badges = React.useMemo(() => {
    const out: Record<string, TabBadge> = {};
    for (const t of state.tabs) {
      if (t.id === state.activeId && front) {
        out[t.id] = front.waiting ? "needs-you" : front.running ? "working" : "none";
        continue;
      }
      const s = status[t.id];
      const mark = (t as WorkflowTab & { mark?: "done" | "unread" }).mark;
      out[t.id] = s?.pendingApproval ? "needs-you" : s?.running ? "working" : mark ?? "none";
    }
    return out;
  }, [state, status, front]);

  const unsaved = React.useMemo(() => {
    const out: Record<string, boolean> = {};
    for (const t of state.tabs) {
      out[t.id] = t.id === state.activeId && front ? front.dirty : !!kept.current.get(t.id)?.dirty;
    }
    return out;
    // `kept` is a ref; `keptVersion` says when it changed.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state, front, keptVersion]);

  return {
    ready, start, make, state, front: activeTab(state), frontReport: front, setFrontReport: setFront,
    kept: kept.current, isFresh: (id: string) => fresh.current.has(id),
    notice, setNotice, badges, unsaved, asking,
    open, openWorkflow, openRun, openNew, focus, close, chooseClose, cancelAsk, move, change, keep,
  };
}
