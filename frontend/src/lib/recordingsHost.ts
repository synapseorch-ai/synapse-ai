import type { RecordingsHost } from '@/components/browser';

const enc = encodeURIComponent;

/**
 * The self-hosted product's way to its Recordings (BRW2, F1142; DESIGN §v5.5): the backend's own `/api/recordings…`
 * and `/api/browser/{sid}/record/*` routes, through the app's catch-all proxy. No plan locks them here.
 */
export const OSS_RECORDINGS_HOST: RecordingsHost = {
  product: 'Synapse',
  listUrl: () => '/api/recordings',
  recordingUrl: (id) => `/api/recordings/${enc(id)}`,
  newUrl: () => '/api/recordings/new',
  shotUrl: (id, ref) => `/api/recordings/${enc(id)}/shots/${enc(ref)}`,
  versionsUrl: (id) => `/api/recordings/${enc(id)}/versions`,
  recordStartUrl: (browserSessionId) => `/api/browser/${enc(browserSessionId)}/record/start`,
  recordStopUrl: (browserSessionId) => `/api/browser/${enc(browserSessionId)}/record/stop`,
  recordingHref: (id) => `/browser/recordings/${enc(id)}`,
  libraryHref: '/browser/recordings',
};
