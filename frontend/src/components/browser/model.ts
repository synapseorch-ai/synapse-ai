/**
 * The browser inside Synapse, as the person's screens see it (BRW2, D330).
 *
 * The browser component (BRW1, `backend/core/browser/`) announces a session on
 * the chat turn stream and the run journal (`browser_session`), then one line per
 * agent action (`browser_action`), and streams the page as JPEG frames. These are
 * those shapes, and the words a line reads in. How the frames are reached is the
 * product's (`BrowserHost`): OSS through its own `/api/browser/…` routes, cloud
 * straight from the session's MicroVM. Synced to cloud with the folder.
 */

/** Who holds the wheel: the agent, the person, or nobody (DESIGN §2.4). */
export type Wheel = 'agent' | 'person' | 'nobody';

/** One screencast frame (`GET …/frames`, DESIGN §6). */
export type BrowserFrame = {
  jpeg_b64: string;
  w: number;
  h: number;
  url: string;
  wheel: Wheel;
};

/** `browser_session`: an agent opened a browser for this chat or run. */
export type BrowserSessionEvent = { type: 'browser_session'; session_id: string; owner: string };

/** `browser_action`: one thing the agent did in the page. Typed values never appear in `target`. */
export type BrowserAction = {
  session_id: string;
  n: number;
  at: number;
  actor: string;
  action: string;
  target: string;
  url: string;
  shot_ref: string | null;
};

export type BrowserEvent = BrowserSessionEvent | ({ type: 'browser_action' } & BrowserAction);

/** How a product reaches a session's frames, screenshots and Stop. */
export type BrowserHost = {
  framesUrl: (sessionId: string) => string;
  shotUrl: (sessionId: string, ref: string) => string;
  /** Ends the agent's turn or run and closes its browser. Resolves false when it could not. */
  stop: (sessionId: string) => Promise<boolean>;
};

export function isBrowserEvent(data: unknown): data is BrowserEvent {
  const type = (data as { type?: unknown } | null)?.type;
  return (type === 'browser_session' || type === 'browser_action')
    && typeof (data as { session_id?: unknown }).session_id === 'string';
}

/** A `browser_action` event's fields, as a log line keeps them. */
export function actionOf(data: Record<string, unknown>): BrowserAction {
  return {
    session_id: String(data.session_id ?? ''),
    n: Number(data.n ?? 0),
    at: Number(data.at ?? 0),
    actor: String(data.actor ?? 'agent'),
    action: String(data.action ?? ''),
    target: String(data.target ?? ''),
    url: String(data.url ?? ''),
    shot_ref: data.shot_ref == null ? null : String(data.shot_ref),
  };
}

const VERBS: Record<string, string> = {
  browser_navigate: 'Opened',
  browser_click: 'Clicked',
  browser_hover: 'Pointed at',
  browser_handle_dialog: 'Answered a dialog',
  browser_navigate_back: 'Went back',
  browser_tabs: 'Switched tabs',
};

/**
 * What one action did, in words: "Clicked Search button", "Opened https://…".
 * The component already words typing, choosing and keys ("typed into Email"),
 * so those only gain a capital.
 */
export function actionWords(a: Pick<BrowserAction, 'action' | 'target' | 'actor'>): string {
  if (a.actor === 'guard') return `Kept from leaving the browser: ${a.target}`;
  const verb = VERBS[a.action];
  if (verb) return a.target && a.action !== 'browser_navigate_back' ? `${verb} ${a.target}` : verb;
  const said = a.target.trim();
  return said ? said[0].toUpperCase() + said.slice(1) : a.action.replace(/^browser_/, '').replace(/_/g, ' ');
}

/** The page's address without its scheme, for a header that has a line's room. */
export function shortUrl(url: string): string {
  return url.replace(/^https?:\/\//, '').replace(/\/$/, '');
}

/** "1 action", "7 actions". */
export function actionCount(n: number): string {
  return `${n} action${n === 1 ? '' : 's'}`;
}
