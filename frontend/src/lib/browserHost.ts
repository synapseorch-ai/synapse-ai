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
  offerFile: (sessionId, attachmentId) => post(`/api/browser/${enc(sessionId)}/file`, { attachment_id: attachmentId }),
};

async function post(url: string, body: unknown): Promise<boolean> {
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).catch(() => null);
  return !!res?.ok;
}
