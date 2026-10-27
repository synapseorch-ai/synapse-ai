import type { BrowserHost } from '@/components/browser';

const enc = encodeURIComponent;

/**
 * The self-hosted product's way to a browser session: the backend's own
 * `/api/browser/{session}/…` routes (BRW1), reached through the app's catch-all
 * proxy, which streams. They answer 404 once the browser is closed.
 */
export const OSS_BROWSER_HOST: BrowserHost = {
  framesUrl: (sessionId) => `/api/browser/${enc(sessionId)}/frames`,
  shotUrl: (sessionId, ref) => `/api/browser/${enc(sessionId)}/shot/${enc(ref)}`,
  keptShotUrl: (owner, id, n) => `/api/browser/kept/${owner}/${enc(id)}/${n}`,
  stop: async (sessionId) => {
    const res = await fetch(`/api/browser/${enc(sessionId)}/stop`, { method: 'POST' });
    // Already closed is stopped too.
    return res.ok || res.status === 404;
  },
  takeWheel: (sessionId) => post(`/api/browser/${enc(sessionId)}/wheel/take`, {}),
  handBack: (sessionId, note) => post(`/api/browser/${enc(sessionId)}/wheel/hand-back`, note ? { note } : {}),
  input: (sessionId, input) => post(`/api/browser/${enc(sessionId)}/input`, input),
  release: (sessionId, allowAddress) => post(`/api/browser/${enc(sessionId)}/release`, { allow_address: allowAddress }),
  allowSite: (sessionId, host) => post(`/api/browser/${enc(sessionId)}/allow-site`, { host }),
  answerDialog: (sessionId, accept, text) =>
    post(`/api/browser/${enc(sessionId)}/dialog`, text === undefined ? { accept } : { accept, text }),
  choose: (sessionId, target, value) => post(`/api/browser/${enc(sessionId)}/select`, { target, value }),
  startLogin: async (sites) => {
    const res = await fetch('/api/browser/login-sessions', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sites }),
    }).catch(() => null);
    const body = await res?.json().catch(() => ({})) ?? {};
    if (res?.ok && typeof body.session_id === 'string') return { sessionId: body.session_id };
    return { refused: typeof body.detail === 'string' ? body.detail : 'The browser could not be opened. Try again.' };
  },
  finishLogin: async (sessionId, keep) => {
    const res = await fetch(`/api/browser/${enc(sessionId)}/finish-login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ keep }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const body = await res.json().catch(() => ({}));
    return typeof body.login_ref === 'string' ? body.login_ref : null;
  },
  offerFile: async (sessionId, file) => {
    const form = new FormData();
    form.append('file', file);
    const res = await fetch(`/api/browser/${enc(sessionId)}/file`, { method: 'POST', body: form }).catch(() => null);
    return !!res?.ok;
  },
};

async function post(url: string, body: unknown): Promise<boolean> {
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).catch(() => null);
  return !!res?.ok;
}
