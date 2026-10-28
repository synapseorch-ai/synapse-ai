import type { SessionsHost } from '@/components/browser';

const enc = encodeURIComponent;

/**
 * The self-hosted product's way to the Sessions library (BRW2, F1139; DESIGN §v5.5): the backend's own
 * `/api/site-sessions…` routes, reached through the app's catch-all proxy. OSS is one person's, so there is no one
 * to share with, and no plan locks it.
 */
export const OSS_SESSIONS_HOST: SessionsHost = {
  listUrl: () => '/api/site-sessions',
  sessionUrl: (id) => `/api/site-sessions/${enc(id)}`,
  newUrl: () => '/api/site-sessions/new',
  finishSessionUrl: (browserSessionId) => `/api/browser/${enc(browserSessionId)}/finish-session`,
  chatPicksUrl: (chatId) => `/api/chat/${enc(chatId)}/browser-sessions`,
  libraryHref: '/browser/sessions',
};
