/*
 * Tabs: what is open, which one is in front, and what each one's badge says
 * (TABS1, D319; F923).
 *
 * Pure, so both products share it through the kit sync and pro's vitest covers
 * it: the strip, the phone sheet and the shortcuts draw from it, and only where
 * tabs are saved differs (localStorage in OSS, the server in cloud).
 *
 * A tab is a view. Opening one runs nothing; closing one stops nothing — a turn
 * keeps going on the server, and the tab that comes back reads it again.
 */

/** The most tabs one area keeps open. Past it, the oldest idle tab closes. */
export const MAX_TABS = 10;

export type TabArea = 'chat' | 'workflows';

export interface Tab {
  id: string;
  /**
   * What the tab shows, unique among the open tabs: a chat tab's session and
   * agent, a workflow's id. Opening something already open focuses its tab.
   */
  key: string;
  title: string;
  /** Milliseconds; the cap closes the tab least recently in front. */
  usedAt: number;
}

export interface ChatTab extends Tab {
  sessionId: string;
  agentId: string;
  /** What the composer held when the tab went to the back. */
  draft?: string;
  /** Set while the tab is behind, cleared when it comes to the front. */
  mark?: TabMark;
  /** It went behind while its turn ran: the next answer there is the person's own (done), not news. */
  awaiting?: boolean;
  /**
   * Opened just now with a new conversation: there is nothing on the server to
   * read back yet. Never saved, so a reloaded tab always reads its conversation.
   */
  fresh?: boolean;
}

/** What happened while a tab was behind. */
export type TabMark = 'done' | 'unread';

export interface TabsState<T extends Tab = Tab> {
  tabs: T[];
  activeId: string | null;
}

export const EMPTY: TabsState<never> = { tabs: [], activeId: null };

export function chatKey(sessionId: string, agentId: string): string {
  return `${agentId}\u0000${sessionId}`;
}

export function activeTab<T extends Tab>(state: TabsState<T>): T | null {
  return state.tabs.find((t) => t.id === state.activeId) ?? null;
}

export type OpenResult<T extends Tab> =
  | { state: TabsState<T>; closed: T | null; refused?: undefined }
  | { state: TabsState<T>; closed: null; refused: string };

/** Said when every open tab is working or waiting on the person and one more is asked for. */
export const ALL_BUSY = `${MAX_TABS} tabs are open and each is working or waiting on you. Close one to open another.`;

/** Said at the cap where no tab is ever closed for the person: a workflow's may hold unsaved work. */
export const AT_CAP = `Close a tab to open another: ${MAX_TABS} are open.`;

/**
 * At the cap: `close-idle` closes the tab least recently in front that isn't
 * busy (chat, where a tab is only a view); `refuse` closes nothing and says so
 * (workflows, where a tab may hold unsaved work).
 */
export type AtCap = 'close-idle' | 'refuse';

/**
 * Open `tab` at the end and bring it to the front, or bring the tab already
 * showing the same thing to the front.
 *
 * At the cap the tab least recently in front closes first — never the one in
 * front, and never one that `busy` says is working or waiting on the person.
 * If every tab is, nothing closes and the open is refused with a sentence.
 */
export function openTab<T extends Tab>(
  state: TabsState<T>,
  tab: T,
  busy: (t: T) => boolean = () => false,
  atCap: AtCap = 'close-idle',
): OpenResult<T> {
  const same = state.tabs.find((t) => t.key === tab.key);
  if (same) return { state: focusTab(state, same.id, tab.usedAt), closed: null };

  let tabs = state.tabs;
  let closed: T | null = null;
  if (tabs.length >= MAX_TABS && atCap === 'refuse') return { state, closed: null, refused: AT_CAP };
  if (tabs.length >= MAX_TABS) {
    const idle = tabs
      .filter((t) => t.id !== state.activeId && !busy(t))
      .sort((a, b) => a.usedAt - b.usedAt);
    if (idle.length === 0) return { state, closed: null, refused: ALL_BUSY };
    closed = idle[0];
    tabs = tabs.filter((t) => t.id !== closed!.id);
  }
  return { state: { tabs: [...tabs, tab], activeId: tab.id }, closed };
}

/** Bring a tab to the front. Its mark clears: the person is looking at it. */
export function focusTab<T extends Tab>(state: TabsState<T>, id: string, now: number): TabsState<T> {
  if (!state.tabs.some((t) => t.id === id)) return state;
  return {
    tabs: state.tabs.map((t) => (t.id === id ? withoutMark({ ...t, usedAt: now }) : t)),
    activeId: id,
  };
}

function withoutMark<T extends Tab>(tab: T): T {
  if (!('mark' in tab) && !('awaiting' in tab)) return tab;
  const next = { ...tab } as T & { mark?: TabMark; awaiting?: boolean };
  delete next.mark;
  delete next.awaiting;
  return next;
}

/**
 * Close a tab. If it was in front, the one to its right comes forward, else
 * the one to its left — as a browser does.
 */
export function closeTab<T extends Tab>(state: TabsState<T>, id: string, now: number): TabsState<T> {
  const at = state.tabs.findIndex((t) => t.id === id);
  if (at < 0) return state;
  const tabs = state.tabs.filter((t) => t.id !== id);
  if (state.activeId !== id) return { tabs, activeId: state.activeId };
  const next = tabs[at] ?? tabs[at - 1] ?? null;
  return next ? focusTab({ tabs, activeId: null }, next.id, now) : { tabs, activeId: null };
}

/** Move a tab to `to` (a drag in the strip). */
export function moveTab<T extends Tab>(state: TabsState<T>, id: string, to: number): TabsState<T> {
  const from = state.tabs.findIndex((t) => t.id === id);
  if (from < 0) return state;
  const tabs = [...state.tabs];
  const [moved] = tabs.splice(from, 1);
  tabs.splice(Math.max(0, Math.min(to, tabs.length)), 0, moved);
  return { ...state, tabs };
}

/** Change one tab in place (its title, draft, agent or mark). */
export function updateTab<T extends Tab>(state: TabsState<T>, id: string, change: Partial<T>): TabsState<T> {
  return { ...state, tabs: state.tabs.map((t) => (t.id === id ? { ...t, ...change } : t)) };
}

/**
 * Tabs read back from storage: anything malformed is dropped, a duplicate of
 * an earlier tab is dropped, at most `MAX_TABS` stay, and the front tab falls
 * back to the first when the saved one is gone. Storage is never trusted:
 * another version of the page, a hand edit, or a full disk wrote it.
 */
export function restoreChatTabs(raw: unknown): TabsState<ChatTab> {
  const value = raw as { tabs?: unknown; activeId?: unknown } | null;
  const list = Array.isArray(value?.tabs) ? value!.tabs : [];
  const tabs: ChatTab[] = [];
  const keys = new Set<string>();
  for (const item of list) {
    const tab = chatTabFrom(item);
    if (!tab || keys.has(tab.key) || tabs.length >= MAX_TABS) continue;
    keys.add(tab.key);
    tabs.push(tab);
  }
  const activeId = typeof value?.activeId === 'string' && tabs.some((t) => t.id === value.activeId)
    ? value.activeId
    : tabs[0]?.id ?? null;
  return { tabs, activeId };
}

const ID = /^[A-Za-z0-9_.:\-]{1,255}$/;

function chatTabFrom(item: unknown): ChatTab | null {
  const t = item as Partial<ChatTab> | null;
  if (!t || typeof t !== 'object') return null;
  if (typeof t.id !== 'string' || !ID.test(t.id)) return null;
  if (typeof t.sessionId !== 'string' || !ID.test(t.sessionId)) return null;
  if (typeof t.agentId !== 'string' || !ID.test(t.agentId)) return null;
  const tab: ChatTab = {
    id: t.id,
    sessionId: t.sessionId,
    agentId: t.agentId,
    key: chatKey(t.sessionId, t.agentId),
    title: typeof t.title === 'string' ? t.title.slice(0, 200) : '',
    usedAt: typeof t.usedAt === 'number' && Number.isFinite(t.usedAt) ? t.usedAt : 0,
  };
  if (typeof t.draft === 'string' && t.draft) tab.draft = t.draft.slice(0, 20_000);
  if (t.mark === 'done' || t.mark === 'unread') tab.mark = t.mark;
  return tab;
}

/** A workflow being edited, or one of its runs beside it (TABS1 v2). */
export interface WorkflowTab extends Tab {
  kind: 'workflow' | 'run';
  /** The workflow's id, or the run's. */
  ref: string;
  /** A run tab's workflow: it opens beside that workflow's tab. */
  workflowId?: string;
}

export function workflowKey(kind: WorkflowTab['kind'], ref: string): string {
  return `${kind}\u0000${ref}`;
}

/** Workflow tabs read back from storage, as `restoreChatTabs` reads chat's. */
export function restoreWorkflowTabs(raw: unknown): TabsState<WorkflowTab> {
  const value = raw as { tabs?: unknown; activeId?: unknown } | null;
  const list = Array.isArray(value?.tabs) ? value!.tabs : [];
  const tabs: WorkflowTab[] = [];
  const keys = new Set<string>();
  for (const item of list) {
    const t = item as Partial<WorkflowTab> | null;
    if (!t || typeof t !== 'object') continue;
    if (t.kind !== 'workflow' && t.kind !== 'run') continue;
    if (typeof t.id !== 'string' || !ID.test(t.id) || typeof t.ref !== 'string' || !ID.test(t.ref)) continue;
    const key = workflowKey(t.kind, t.ref);
    if (keys.has(key) || tabs.length >= MAX_TABS) continue;
    keys.add(key);
    tabs.push({
      id: t.id, kind: t.kind, ref: t.ref, key,
      title: typeof t.title === 'string' ? t.title.slice(0, 200) : '',
      usedAt: typeof t.usedAt === 'number' && Number.isFinite(t.usedAt) ? t.usedAt : 0,
      ...(typeof t.workflowId === 'string' && ID.test(t.workflowId) ? { workflowId: t.workflowId } : {}),
    });
  }
  const activeId = typeof value?.activeId === 'string' && tabs.some((t) => t.id === value.activeId)
    ? value.activeId
    : tabs[0]?.id ?? null;
  return { tabs, activeId };
}

/** What one status read says about a tab's conversation (`/sessions/status`). */
export interface TabStatus {
  running: boolean;
  /** A running turn the person didn't send: a helper's follow-up, a resumed approval. */
  automated?: boolean;
  pendingApproval: boolean;
  turnCount: number;
  lastUpdated: string | null;
}

/**
 * The mark a tab behind earns between two status reads.
 *
 * done — a turn the person sent finished while they were on another tab;
 * unread — the conversation moved on with a message they didn't start.
 * A tab in front earns nothing: the person is reading it.
 */
export function markBetween(
  before: TabStatus | undefined, now: TabStatus, inFront: boolean, awaiting = false,
): TabMark | null {
  if (inFront || !before || now.running) return null;
  if (before.running) return before.automated && !awaiting ? 'unread' : 'done';
  // A turn that started and ended between two reads: the conversation grew.
  if (now.turnCount > before.turnCount) return awaiting ? 'done' : 'unread';
  return null;
}

export type TabBadge = 'needs-you' | 'working' | 'unread' | 'done' | 'none';

/**
 * One badge per tab, the most pressing first: a question waiting on the
 * person, then work under way, then news.
 */
export function badgeOf(status: TabStatus | undefined, mark: TabMark | undefined): TabBadge {
  if (status?.pendingApproval) return 'needs-you';
  if (status?.running) return 'working';
  return mark ?? 'none';
}

/** Words for a badge, for a screen reader and the phone sheet. */
export const BADGE_WORDS: Record<TabBadge, string> = {
  'needs-you': 'needs you',
  working: 'working',
  unread: 'new answer',
  done: 'finished',
  none: '',
};

/** A tab that the cap may not close: working, or waiting on the person. */
export function isBusy(status: TabStatus | undefined): boolean {
  return !!status && (status.running || status.pendingApproval);
}

/**
 * Which tabs the strip shows when only `fits` of them fit: the first ones, in
 * order, with the tab in front always among them (it takes the last place if
 * it would otherwise be hidden). The rest go behind "N more".
 */
export function splitVisible<T extends Tab>(state: TabsState<T>, fits: number): { shown: T[]; more: T[] } {
  const n = Math.max(1, Math.floor(fits));
  if (state.tabs.length <= n) return { shown: state.tabs, more: [] };
  let shown = state.tabs.slice(0, n);
  const active = activeTab(state);
  if (active && !shown.includes(active)) shown = [...shown.slice(0, n - 1), active];
  return { shown, more: state.tabs.filter((t) => !shown.includes(t)) };
}

export type TabShortcut =
  | { kind: 'new' }
  | { kind: 'close' }
  | { kind: 'jump'; index: number };

/**
 * Alt+T, Alt+W and Alt+1…9. Ctrl/Cmd+T and Ctrl+W belong to the browser.
 * Read from `code`, not `key`: Alt on a Mac types "†" for T.
 */
export function shortcutOf(e: {
  altKey: boolean; ctrlKey: boolean; metaKey: boolean; shiftKey: boolean; code: string; isComposing?: boolean;
}): TabShortcut | null {
  if (!e.altKey || e.ctrlKey || e.metaKey || e.shiftKey || e.isComposing) return null;
  if (e.code === 'KeyT') return { kind: 'new' };
  if (e.code === 'KeyW') return { kind: 'close' };
  const digit = /^Digit([1-9])$/.exec(e.code);
  return digit ? { kind: 'jump', index: Number(digit[1]) - 1 } : null;
}
