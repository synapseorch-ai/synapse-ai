"use client";

/*
 * A chat page's tabs, as one hook both products share (TABS1, F923–F925).
 *
 * What is open, which is in front, what each tab's badge says, the pins, and
 * the rules: a conversation already open is brought forward rather than opened
 * twice, a new tab is a new conversation — Synapse AI's too (TABS2) — ten at
 * most with the oldest idle one closing first, and chat always has a tab. Each product brings only its adapters:
 * where tabs are kept (the browser in OSS, the server in cloud), how the tabs
 * behind are read, and how a new conversation is named before its first turn.
 */

import * as React from "react";

import {
  badgeOf,
  chatKey,
  closeTab,
  focusTab,
  isBusy,
  moveTab,
  openTab,
  updateTab,
  activeTab,
  type ChatTab,
  type TabBadge,
  type TabStatus,
  type TabsState,
} from "./model";
import { useTabStatus } from "./useTabStatus";

/** The most conversations pinned; past it, the oldest pin goes. */
export const MAX_PINS = 50;

export type Pin = { sessionId: string; agentId: string };

export interface SavedChatTabs {
  state: TabsState<ChatTab>;
  pins: Pin[];
}

/** How long the tabs wait after a change before they are kept: a drag or a burst of badges is one save. */
const SAVE_AFTER_MS = 400;

export function useChatTabs({
  load,
  save,
  readStatus,
  newSessionId,
}: {
  /** What was kept, read once. `null` when nothing was. */
  load: () => Promise<SavedChatTabs | null>;
  save: (saved: SavedChatTabs) => void;
  readStatus: (tabs: ChatTab[]) => Promise<Record<string, TabStatus>>;
  /** A new conversation's session id: minted here in OSS, a placeholder in cloud until its first turn. */
  newSessionId: () => string;
}) {
  const [ready, setReady] = React.useState(false);
  const [state, setState] = React.useState<TabsState<ChatTab>>({ tabs: [], activeId: null });
  const [pins, setPins] = React.useState<Pin[]>([]);
  const [frontBusy, setFrontBusy] = React.useState(false);
  const [notice, setNotice] = React.useState<string | null>(null);

  const adapters = React.useRef({ load, save, newSessionId });
  React.useEffect(() => { adapters.current = { load, save, newSessionId }; });

  const make = React.useCallback((sessionId: string, agentId: string, title = "", fresh = false): ChatTab => ({
    id: `tab_${adapters.current.newSessionId().replace(/[^A-Za-z0-9_.:-]/g, "")}`,
    sessionId,
    agentId,
    key: chatKey(sessionId, agentId),
    title,
    usedAt: Date.now(),
    ...(fresh ? { fresh } : {}),
  }), []);

  /**
   * Read what was kept, once. `seed` decides what opens when nothing was, or
   * adds to what was (a conversation the page before tabs left behind).
   */
  const start = React.useCallback(async (seed: (kept: TabsState<ChatTab>) => TabsState<ChatTab>) => {
    let kept: SavedChatTabs | null = null;
    try { kept = await adapters.current.load(); } catch { /* nothing kept is a fresh start */ }
    setState(seed(kept?.state ?? { tabs: [], activeId: null }));
    setPins(kept?.pins ?? []);
    setReady(true);
  }, []);

  // Kept a moment after each change.
  React.useEffect(() => {
    if (!ready) return;
    const wait = setTimeout(() => adapters.current.save({ state, pins }), SAVE_AFTER_MS);
    return () => clearTimeout(wait);
  }, [ready, state, pins]);

  const { status, loaded: statusLoaded } = useTabStatus({
    tabs: state.tabs,
    activeId: state.activeId,
    read: readStatus,
    // Earned: the mark shows, and a tab that was awaiting its answer has it.
    onMark: (id, mark) => setState((s) => updateTab(s, id, { mark, awaiting: false })),
  });

  const badges = React.useMemo(() => {
    const out: Record<string, TabBadge> = {};
    for (const t of state.tabs) {
      out[t.id] = t.id === state.activeId
        // The tab in front: its own screen is the truth for work; a waiting question is the server's.
        ? (status[t.id]?.pendingApproval ? "needs-you" : frontBusy ? "working" : "none")
        : badgeOf(status[t.id], t.mark);
    }
    return out;
  }, [state, status, frontBusy]);

  const statusRef = React.useRef(status);
  React.useEffect(() => { statusRef.current = status; }, [status]);
  const frontBusyRef = React.useRef(frontBusy);
  React.useEffect(() => { frontBusyRef.current = frontBusy; }, [frontBusy]);

  /** The tab in front goes behind: if its turn is running, the answer coming there is the person's own. */
  const leaving = React.useCallback((s: TabsState<ChatTab>): TabsState<ChatTab> =>
    s.activeId && frontBusyRef.current ? updateTab(s, s.activeId, { awaiting: true }) : s, []);

  /** Open `tab`, or bring forward the one already showing its conversation; at the cap, say why not. */
  const open = React.useCallback((tab: ChatTab) => {
    setState((s) => {
      const result = openTab(leaving(s), tab, (t) => isBusy(statusRef.current[t.id]));
      if (result.refused) queueMicrotask(() => setNotice(result.refused));
      return result.state;
    });
    setNotice(null);
    setFrontBusy(false);
  }, [leaving]);

  /**
   * A new conversation with `agentId`, Synapse AI's included: each of its chats is its own (TABS2, F1061). `draft`
   * waits in the composer, unsent; `suggested` says a link put it there, not the person.
   */
  const openAgent = React.useCallback((agentId: string, words?: { draft?: string; suggested?: boolean }) => {
    setState((s) => {
      const tab = make(adapters.current.newSessionId(), agentId, "", true);
      const result = openTab(leaving(s), { ...tab, ...(words?.draft ? { draft: words.draft, suggested: Boolean(words.suggested) } : {}) },
        (t) => isBusy(statusRef.current[t.id]));
      if (result.refused) queueMicrotask(() => setNotice(result.refused));
      return result.state;
    });
    setFrontBusy(false);
  }, [make, leaving]);

  /** A conversation from History, a pin, a search or a link. */
  const openConversation = React.useCallback((sessionId: string, agentId: string, title = "") => {
    open(make(sessionId, agentId, title));
  }, [open, make]);

  const focus = React.useCallback((id: string) => {
    setState((s) => (s.activeId === id ? s : focusTab(leaving(s), id, Date.now())));
    setFrontBusy(false);
    setNotice(null);
  }, [leaving]);

  const close = React.useCallback((id: string) => {
    setNotice(null);
    setState((s) => {
      const next = closeTab(s, id, Date.now());
      if (next.tabs.length > 0) return next;
      // Chat always has a tab: closing the last opens a new one with the same agent.
      const last = s.tabs.find((t) => t.id === id);
      return last ? openTab(next, make(adapters.current.newSessionId(), last.agentId, "", true)).state : next;
    });
    setFrontBusy(false);
  }, [make]);

  const move = React.useCallback((id: string, to: number) => setState((s) => moveTab(s, id, to)), []);

  /** The front conversation's title, draft, or — before its first turn — its agent or session changed. */
  const change = React.useCallback((id: string, edit: Partial<ChatTab>) => {
    setState((s) => {
      const tab = s.tabs.find((t) => t.id === id);
      if (!tab) return s;
      const sessionId = edit.sessionId ?? tab.sessionId;
      const agentId = edit.agentId ?? tab.agentId;
      return updateTab(s, id, { ...edit, key: chatKey(sessionId, agentId) });
    });
  }, []);

  const togglePin = React.useCallback((pin: Pin) => setPins((p) => (
    p.some((x) => x.sessionId === pin.sessionId && x.agentId === pin.agentId)
      ? p.filter((x) => !(x.sessionId === pin.sessionId && x.agentId === pin.agentId))
      : [pin, ...p].slice(0, MAX_PINS)
  )), []);

  return {
    ready, start, make, state, front: activeTab(state), pins, status, statusLoaded, badges,
    frontBusy, setFrontBusy, notice, setNotice,
    open, openAgent, openConversation, focus, close, move, change, togglePin,
  };
}

/** Pins read back from storage: anything malformed dropped, the cap kept. */
export function restorePins(raw: unknown): Pin[] {
  const list = Array.isArray((raw as { pins?: unknown } | null)?.pins) ? (raw as { pins: unknown[] }).pins : [];
  return list
    .filter((p): p is Pin => typeof (p as Pin)?.sessionId === "string" && typeof (p as Pin)?.agentId === "string")
    .slice(0, MAX_PINS)
    .map((p) => ({ sessionId: p.sessionId, agentId: p.agentId }));
}
