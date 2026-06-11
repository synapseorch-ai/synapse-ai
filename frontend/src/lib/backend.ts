/**
 * Shared backend communication helpers.
 * Provides the internal token header for frontend→backend requests.
 */

const BACKEND_URL = process.env.BACKEND_URL || 'http://127.0.0.1:8765';

/**
 * The internal token is generated per-install at runtime, so it must be read
 * from the live process.env at call time — NOT captured at module load and NOT
 * inlined at build time (see next.config.ts). Reading lazily also guards against
 * the env var being populated after this module is first imported.
 */
function getInternalToken(): string {
    return process.env.SYNAPSE_INTERNAL_TOKEN || '';
}

/**
 * Returns headers object with the internal token for backend requests.
 * Merge with any additional headers you need.
 */
export function backendHeaders(extra?: Record<string, string>): Record<string, string> {
    const headers: Record<string, string> = {
        'Content-Type': 'application/json',
    };
    const token = getInternalToken();
    if (token) {
        headers['X-Synapse-Internal'] = token;
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
    const token = getInternalToken();
    if (!token) return {};
    return { 'X-Synapse-Internal': token };
}

export { BACKEND_URL, getInternalToken };
