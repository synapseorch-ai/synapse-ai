/**
 * Shared backend communication helpers.
 * Provides the internal token header for frontend→backend requests.
 */

const BACKEND_URL = process.env.BACKEND_URL || 'http://127.0.0.1:8765';
const INTERNAL_TOKEN = process.env.SYNAPSE_INTERNAL_TOKEN || '';

/**
 * Returns headers object with the internal token for backend requests.
 * Merge with any additional headers you need.
 */
export function backendHeaders(extra?: Record<string, string>): Record<string, string> {
    const headers: Record<string, string> = {
        'Content-Type': 'application/json',
    };
    if (INTERNAL_TOKEN) {
        headers['X-Synapse-Internal'] = INTERNAL_TOKEN;
    }
    if (extra) {
        Object.assign(headers, extra);
    }
    return headers;
}

/**
 * Returns just the internal token header for http.request() options.
 */
export function internalTokenHeader(): Record<string, string> {
    if (!INTERNAL_TOKEN) return {};
    return { 'X-Synapse-Internal': INTERNAL_TOKEN };
}

export { BACKEND_URL, INTERNAL_TOKEN };
