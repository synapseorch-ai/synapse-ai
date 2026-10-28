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
  keptLogUrl: (owner, id) => `/api/browser/kept/${owner}/${enc(id)}/log`,
  stop: async (sessionId) => {
    const res = await fetch(`/api/browser/${enc(sessionId)}/stop`, { method: 'POST' });
    // Already closed is stopped too.
    return res.ok || res.status === 404;
  },
  takeWheel: (sessionId) => post(`/api/browser/${enc(sessionId)}/wheel/take`, {}),
  handBack: async (sessionId, note) => {
    const res = await fetch(`/api/browser/${enc(sessionId)}/wheel/hand-back`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(note ? { note } : {}),
    }).catch(() => null);
    if (res?.status === 422) {
      const body = await res.json().catch(() => ({}));
      throw new Error(typeof body.detail === 'string' ? body.detail : 'That note was not taken.');
    }
    if (!res?.ok) return false;
    // A session that expired, signed in again in this browser, is kept under the same entry (BRW1, F1138).
    const body = await res.json().catch(() => ({}));
    return body?.kept && typeof body.kept.id === 'string' ? { kept: body.kept } : true;
  },
  input: (sessionId, input) => post(`/api/browser/${enc(sessionId)}/input`, input),
  release: (sessionId, allowAddress) => post(`/api/browser/${enc(sessionId)}/release`, { allow_address: allowAddress }),
  allowSite: (sessionId, host) => post(`/api/browser/${enc(sessionId)}/allow-site`, { host }),
  answerDialog: (sessionId, accept, text) =>
    post(`/api/browser/${enc(sessionId)}/dialog`, text === undefined ? { accept } : { accept, text }),
  choose: (sessionId, target, value) => post(`/api/browser/${enc(sessionId)}/select`, { target, value }),
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
