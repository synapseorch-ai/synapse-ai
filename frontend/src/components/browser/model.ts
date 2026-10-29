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

import type { KeptSession } from './sessions';

/** Who holds the wheel: the agent, the person, or nobody (DESIGN §2.4). */
export type Wheel = 'agent' | 'person' | 'nobody';

/** What the person is asked to handle, in the strip under the address (BRW1's v2 contract). */
export type BannerKind = 'send' | 'sensitive' | 'captcha' | 'asked' | 'session_expired' | 'allow_typing' | 'press';
const BANNER_KINDS: readonly string[] = ['send', 'sensitive', 'captcha', 'asked', 'session_expired', 'allow_typing', 'press'];

/**
 * A banner's kind as the screens read it. A component a release behind still says `login_expired` (F1138 renamed it
 * `session_expired`, with one release of grace, DESIGN §v5.5), so it is read as that wherever a kind is shown.
 */
export function bannerKindOf(raw: unknown): BannerKind | undefined {
  if (raw === 'login_expired') return 'session_expired';
  return typeof raw === 'string' && BANNER_KINDS.includes(raw) ? raw as BannerKind : undefined;
}
/**
 * A final button the agent stopped at (BRW1's "Final buttons and sends", Naveen 2026-10-28): one that acts for the
 * person, which the agent presses only on their own words, or one with money in it, which is always theirs.
 */
export type PressAsk = { label: string; does: string; host: string; tier: 'acts' | 'money' };
export type BrowserBanner = {
  kind: BannerKind;
  reason: string;
  /**
   * A send the agent tried, paused at the network until the person presses the page's own button. `money`: a
   * payment, which is never let through (`/release` answers 409), so the person takes over and presses it.
   */
  held?: { method: string; url: string; money?: boolean };
  /** For `allow_typing`: the site it wants to type on. */
  host?: string;
  /** For `press`: the button, what it does, and whether money is in it. */
  press?: PressAsk;
  /** For `session_expired`: the site, its session, and whether it is the person's own to sign in again. */
  site?: string;
  site_session_id?: string;
  mine?: boolean;
};

/** The page's own alert, confirm or prompt, which the screencast cannot show. */
export type PageDialog = { type: 'alert' | 'confirm' | 'prompt' | 'beforeunload'; message: string; default?: string };
/** A native dropdown the page opened: its popup is the browser's, not the page's, so it is never in a frame. */
export type PageSelect = { target: string; options: { value: string; label: string }[]; selected?: string };
/** A file picker the page opened. */
export type PageFile = { multiple: boolean; accept?: string };

/** One screencast frame (`GET …/frames`, DESIGN §6). */
export type BrowserFrame = {
  jpeg_b64: string;
  w: number;
  h: number;
  url: string;
  wheel: Wheel;
  banner?: BrowserBanner;
  dialog?: PageDialog;
  select?: PageSelect;
  file?: PageFile;
  /** A browser recording a task (F1141): how many steps, the last, and a field whose typing isn't being kept. */
  recording?: FrameRecording;
};

export type FrameRecording = { steps: number; last?: { id: string; title: string }; holding?: 'password' | 'card' | 'code' };

/** `browser_session`: an agent opened a browser for this chat or run. */
/** Where a session a browser started with was picked (the nearer level wins on a site; DESIGN v5.1). */
export type SessionLevel = 'agent' | 'chat' | 'run' | 'step';
/** A session a browser could not start with: it expired, or it is someone else's and not shared (v5-7). */
export type SkippedSession = { site: string; reason: 'expired' | 'not_shared'; owner_name: string };

/**
 * `browser_session`: an agent opened a browser for this chat or run, signed in with `sessions` (F1140; absent from a
 * component before it).
 */
export type BrowserSessionEvent = {
  type: 'browser_session';
  session_id: string;
  owner: string;
  sessions?: { id: string; site: string; level: SessionLevel }[];
  skipped?: SkippedSession[];
};

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
  /** A final press by the agent: the person's own words that asked for it. */
  because?: string;
};

export type BrowserEvent = BrowserSessionEvent | ({ type: 'browser_action' } & BrowserAction);

/** What the person does to the page while they hold the wheel (`POST …/input`), in page CSS pixels. */
export type BrowserInput =
  | { type: 'mouse'; event: 'click' | 'down' | 'up' | 'move'; x: number; y: number;
      button: 'left' | 'right' | 'middle'; clicks: number; modifiers: string[] }
  | { type: 'scroll'; x: number; y: number; dx: number; dy: number }
  | { type: 'key'; key: string; modifiers: string[] }
  | { type: 'text'; text: string }
  | { type: 'nav'; url: string }
  | { type: 'nav'; action: 'back' | 'forward' | 'reload' };

/**
 * How a product reaches a session's frames, screenshots and Stop, and, where the person may drive, the wheel and
 * the page's input. A host without the wheel's calls gives a pane the person only watches (BRW1's v1).
 */
/** A hand-back: done or not, or done with the session the person signed in again with kept. */
/**
 * What a hand-back answered: done, or what came of it. `kept`: a session signed in again, kept (F1140). `resumed`: the
 * turn the server started on its own when the person handed back (cloud, BRW3): a chat's next turn to follow, as a
 * sent one is, or the run that carries on. OSS resumes its own way and sends neither.
 */
export type HandedBack = boolean | { kept?: KeptSession; resumed?: ResumedTurn };

export type ResumedTurn =
  | { kind: 'chat'; session_id: string; stream_url: string; cursor: string; turn?: string }
  | { kind: 'run'; run_id: string };

export type BrowserHost = {
  framesUrl: (sessionId: string) => string;
  shotUrl: (sessionId: string, ref: string) => string;
  /** The screenshot `shot_ref` names, kept with its chat or run once the browser closed (the filmstrip). */
  keptShotUrl?: (owner: 'chat' | 'run', id: string, shotRef: number) => string;
  /**
   * The whole log kept with its chat or run once the browser closed, `{entries}`: the agent's lines and the
   * person's own, the record of who pressed what (BRW1, F1125).
   */
  keptLogUrl?: (owner: 'chat' | 'run', id: string) => string;
  /** Ends the agent's turn or run and closes its browser. Resolves false when it could not. */
  stop: (sessionId: string) => Promise<boolean>;
  /** The person takes the wheel: the agent's action in flight finishes and its next one waits. */
  takeWheel?: (sessionId: string) => Promise<boolean>;
  /**
   * The wheel goes back to the agent, with what the person wants it to know. Resolves false when it could not; a
   * note refused (a card number in it, BRW1) rejects with an Error whose message is the refusal, to show as it is.
   * When the person signed in again on a turn for a session that expired, the fresh session is kept under the same
   * entry, and it resolves what was kept (BRW1, F1138).
   */
  handBack?: (sessionId: string, note?: string) => Promise<HandedBack>;
  /** One input to the page; refused (false) unless the person holds the wheel. */
  input?: (sessionId: string, input: BrowserInput) => Promise<boolean>;
  /** "It only loaded data": the held request carries on, and with `allowAddress` that address is not held again. */
  release?: (sessionId: string, allowAddress: boolean) => Promise<boolean>;
  /** Typing on `host` is allowed for the rest of this session. */
  allowSite?: (sessionId: string, host: string) => Promise<boolean>;
  /** The answer to the page's own dialog. */
  answerDialog?: (sessionId: string, accept: boolean, text?: string) => Promise<boolean>;
  /** The option picked in the page's own dropdown. */
  choose?: (sessionId: string, target: string, value: string) => Promise<boolean>;
  /** A file the person picked from their own computer, for the chooser the page has open. */
  offerFile?: (sessionId: string, file: File) => Promise<boolean>;
};

/** A run's browser turn, as its waiting step's fields carry it (BRW1: no column of its own; the field's type says). */
export type RunBrowserTurn = { session_id: string; reason: string; banner_kind?: BannerKind };

export function browserTurnOf(fields: unknown): RunBrowserTurn | null {
  if (!Array.isArray(fields)) return null;
  const f = fields.find((x) => x && typeof x === 'object' && (x as { type?: unknown }).type === 'browser_turn') as
    { session_id?: unknown; label?: unknown; banner_kind?: unknown } | undefined;
  if (!f || typeof f.session_id !== 'string') return null;
  return { session_id: f.session_id, reason: String(f.label ?? ''), banner_kind: bannerKindOf(f.banner_kind) };
}

/**
 * The browser turn a chat waits on, as its history read carries it (`pending_browser_turn`, F1182): a chat reopened
 * while parked shows its card again. None when it is not one.
 */
export function parkedBrowserTurn(raw: unknown): RunBrowserTurn | null {
  if (!raw || typeof raw !== 'object') return null;
  const t = raw as { session_id?: unknown; reason?: unknown; banner_kind?: unknown };
  if (typeof t.session_id !== 'string' || !t.session_id) return null;
  return { session_id: t.session_id, reason: String(t.reason ?? ''), banner_kind: bannerKindOf(t.banner_kind) };
}

/** The site a URL is on, for a sentence. */
export function hostOf(url: string): string {
  try {
    return new URL(url).hostname;
  } catch {
    return url;
  }
}

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
    ...(data.because ? { because: String(data.because) } : {}),
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
export function actionWords(a: Pick<BrowserAction, 'action' | 'target' | 'actor' | 'because'>): string {
  // The guard: what it kept from leaving, and a payment it held for the person to press.
  if (a.actor === 'guard') return a.action === 'held' ? `Held for you: ${a.target}` : `Kept from leaving the browser: ${a.target}`;
  // A final button, pressed by the agent on the person's words or by the person after a turn.
  if (a.action === 'pressed') {
    if (a.actor === 'person') return `You pressed ‘${a.target}’`;
    return a.because ? `Pressed ‘${a.target}’, as you asked: “${a.because}”` : `Pressed ‘${a.target}’`;
  }
  if (a.action === 'took over') return 'You took over';
  // Its target is how long the person drove ("after 12 s"); the record never holds their own words (BRW1).
  if (a.action === 'handed back') {
    return /^after \d/.test(a.target) ? `You handed the browser back ${a.target}` : 'You handed the browser back';
  }
  // What left the browser while the person drove: "POST shop.example.com", never a body or a field.
  if (a.action === 'sent') {
    const [method, ...rest] = a.target.trim().split(/\s+/);
    return rest.length ? `You sent ${method} to ${rest.join(' ')}` : `You sent ${a.target}`;
  }
  const verb = VERBS[a.action];
  if (verb) return a.target && a.action !== 'browser_navigate_back' ? `${verb} ${a.target}` : verb;
  const said = a.target.trim();
  return said ? said[0].toUpperCase() + said.slice(1) : a.action.replace(/^browser_/, '').replace(/_/g, ' ');
}

/**
 * One list of what was done in a browser, from where its lines arrive: the agent's on its `browser_action` events,
 * the person's and the guard's on the frames stream. A line is its session's `n`, so one heard twice is one line.
 */
export function mergeActions(...lists: BrowserAction[][]): BrowserAction[] {
  const seen = new Map<string, BrowserAction>();
  for (const list of lists) for (const a of list) seen.set(`${a.session_id}:${a.n}`, a);
  return [...seen.values()].sort((a, b) => (a.session_id === b.session_id ? a.n - b.n : a.at - b.at));
}

/** The page's address without its scheme, for a header that has a line's room. */
export function shortUrl(url: string): string {
  return url.replace(/^https?:\/\//, '').replace(/\/$/, '');
}

/** "1 action", "7 actions". */
export function actionCount(n: number): string {
  return `${n} action${n === 1 ? '' : 's'}`;
}
