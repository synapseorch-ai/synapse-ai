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
};
