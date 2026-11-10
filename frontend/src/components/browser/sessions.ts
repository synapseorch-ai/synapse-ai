/**
 * Sessions: a site's signed-in state kept for the person's agents (BRW2, D330, F1138–F1140; DESIGN v5.1). Never a
 * password: the person types that in a browser of their own, and what is kept is the site's session, for 30 days at
 * most. These are the shapes frozen in DESIGN §v5.5 (agreed 2026-10-29), and the words a row reads in.
 *
 * How they are reached is the product's (`SessionsHost`): OSS through its `/api/site-sessions…` routes, cloud through
 * its own paths (its `/api/*` is the public API). Synced to cloud with the folder.
 */

export type SessionUses = { agents: number; chats: number; workflows: number };

/** A row of the Sessions library (`GET …/site-sessions`). `site` is the server's registrable domain. */
export type SiteSession = {
  id: string;
  site: string;
  owner: string;
  owner_name: string;
  /** Cloud only: named people, or the whole workspace. Always `[]` in OSS. */
  shared_with: string[] | 'workspace';
  created_at: number;
  last_used_at: number | null;
  expires_at: number;
  expired: boolean;
  mine: boolean;
  /**
   * The person may pick it (F1167; v5-7): theirs, or shared with them or the workspace. Another's unshared session is
   * listed, so the library shows it exists, but never offered. A server without it yet: theirs alone.
   */
  usable?: boolean;
  uses: SessionUses;
};

export function mayUse(s: Pick<SiteSession, 'usable' | 'mine'>): boolean {
  return s.usable ?? s.mine;
}

/**
 * A host is on a session's site: the site itself or one of its subdomains (`site` is the server's registrable domain;
 * a listed `*.example.com` counts as example.com).
 */
export function onSite(host: string, site: string): boolean {
  const h = host.toLowerCase().replace(/^\*\./, '');
  const s = site.toLowerCase();
  return !!h && !!s && (h === s || h.endsWith(`.${s}`));
}

/** "25 Oct": a session's date, as the pane and the pickers both say it. */
export function shortDate(at: number): string {
  return new Date(at * 1000).toLocaleDateString(undefined, { day: 'numeric', month: 'short' });
}

/** What a keep returned: the row, or why nothing was kept (422: nothing is signed in yet). */
export type KeptSession = Pick<SiteSession, 'id' | 'site' | 'expires_at'>;

export type SessionsHost = {
  /** The product's name, as the kit says it ("Synapse", cloud's "SynapseOrch"): the host's, never the kit's own. */
  product: string;
  listUrl: () => string;
  /** One session: DELETE removes it, PATCH shares it (cloud). */
  sessionUrl: (id: string) => string;
  /** POST `{site}` → `{browser_session_id}`: a browser for the person alone, to sign in. */
  newUrl: () => string;
  /** POST `{keep}` → the kept row, or 422 `{detail, site}`; either way but a 422, the browser closes. */
  finishSessionUrl: (browserSessionId: string) => string;
  /** GET / PUT `{ids}`: the sessions picked for one chat. */
  chatPicksUrl: (chatId: string) => string;
  /** Where the library is, for the pickers' "Manage sessions". */
  libraryHref: string;
  /** Sharing with named people or the workspace; OSS has no one to share with. */
  canShare?: boolean;
  /** The plan has no sessions (Free, DESIGN v5-11): the library and pickers say where they come with. */
  locked?: { upgradeHref: string };
};

export async function listSessions(host: SessionsHost): Promise<SiteSession[]> {
  const res = await fetch(host.listUrl());
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const body = await res.json();
  return Array.isArray(body?.sessions) ? body.sessions : [];
}

/** Removed, or already gone. */
export async function removeSession(host: SessionsHost, id: string): Promise<boolean> {
  const res = await fetch(host.sessionUrl(id), { method: 'DELETE' }).catch(() => null);
  return !!res && (res.ok || res.status === 404);
}

/**
 * Opens the person's own browser to sign in to `site`; `start`, the address as typed, is the page it opens on (F1514):
 * the server opens it only on that very host, over https, and otherwise the host's front page.
 */
export async function startSignIn(host: SessionsHost, site: string, start?: string): Promise<{ browserSessionId: string } | { refused: string }> {
  const res = await fetch(host.newUrl(), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(start ? { site, start } : { site }),
  }).catch(() => null);
  const body = await res?.json().catch(() => ({})) ?? {};
  if (res?.ok && typeof body.browser_session_id === 'string') return { browserSessionId: body.browser_session_id };
  return { refused: refusal(body, 'The browser could not be opened. Try again.') };
}

/**
 * Ends a sign-in browser; `keep` keeps its session. A 422 is the person's to fix in the same browser (nothing is
 * signed in yet), so it comes back as `refused` and the browser stays open.
 */
export async function finishSignIn(host: SessionsHost, browserSessionId: string, keep: boolean):
    Promise<{ kept: KeptSession | null } | { refused: string }> {
  const res = await fetch(host.finishSessionUrl(browserSessionId), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ keep }),
  });
  const body = await res.json().catch(() => ({}));
  if (res.status === 422) return { refused: refusal(body, 'Nothing is signed in yet. Sign in, then keep it.') };
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return { kept: keep && typeof body?.id === 'string' ? body as KeptSession : null };
}

/**
 * A refusal's words. The frozen shape is `{detail, site}`; FastAPI nests a dict detail as `{detail: {detail, site}}`,
 * so either is read, and anything else is the fallback.
 */
export function refusal(body: { detail?: unknown } | null | undefined, fallback: string): string {
  const d = body?.detail;
  if (typeof d === 'string') return d;
  if (d && typeof d === 'object' && typeof (d as { detail?: unknown }).detail === 'string') return (d as { detail: string }).detail;
  return fallback;
}

const DAY_S = 86_400;

/** "Expires today", "Expires tomorrow", "Expires in 12 days"; "Expired" once it has. */
export function expiresIn(s: Pick<SiteSession, 'expires_at' | 'expired'>, now = Date.now() / 1000): string {
  if (s.expired || s.expires_at <= now) return 'Expired';
  const days = Math.floor((s.expires_at - now) / DAY_S);
  return days <= 0 ? 'Expires today' : days === 1 ? 'Expires tomorrow' : `Expires in ${days} days`;
}

/** "Last used 5 minutes ago", "Last used 3 days ago"; "Not used yet". */
export function lastUsed(at: number | null, now = Date.now() / 1000): string {
  if (!at) return 'Not used yet';
  const s = Math.max(0, now - at);
  const [n, unit] = s < 3_600 ? [Math.max(1, Math.round(s / 60)), 'minute']
    : s < DAY_S ? [Math.round(s / 3_600), 'hour'] : [Math.round(s / DAY_S), 'day'];
  return `Last used ${n} ${unit}${n === 1 ? '' : 's'} ago`;
}

/** "Used by 2 agents and 1 workflow"; empty when nothing picks it. */
export function usesWords(uses: SessionUses | undefined): string {
  if (!uses) return '';
  const parts = ([['agent', uses.agents], ['chat', uses.chats], ['workflow', uses.workflows]] as const)
    .filter(([, n]) => n > 0)
    .map(([noun, n]) => `${n} ${noun}${n === 1 ? '' : 's'}`);
  if (!parts.length) return '';
  const last = parts.pop();
  return `Used by ${parts.length ? `${parts.join(', ')} and ${last}` : last}`;
}

/** Whose a session is, as its row says it: "Yours", "Priya's session". */
export function whose(s: Pick<SiteSession, 'mine' | 'owner_name'>): string {
  return s.mine ? 'Yours' : `${s.owner_name}’s session`;
}
