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
 * The signed-in person's session, forwarded to the backend (S9).
 *
 * With login on, the backend asks every internal request for a session as well
 * as the internal token: the token proves a call came through this app, not
 * that a signed-in person made it, and the proxy's login gate had been skipped
 * for a cookie any client could forge. A route handler's own `fetch` carries
 * none of the browser's cookies, so these read `synapse_session` from the
 * request and send it as `X-Synapse-Session`.
 *
 * Async, and named differently from the synchronous helpers they replaced, so a
 * missing `await` is a type error rather than a spread of a Promise that quietly
 * drops the token. Call them in the handler, not inside a stream callback: the
 * request's cookies are only readable in its scope.
 */
async function sessionToken(): Promise<string> {
    const { cookies } = await import('next/headers');
    return (await cookies()).get('synapse_session')?.value ?? '';
}

/** Content-Type, the internal token and the session, for a backend `fetch`. */
export async function sessionHeaders(extra?: Record<string, string>): Promise<Record<string, string>> {
    const headers: Record<string, string> = {
        'Content-Type': 'application/json',
        ...(await internalHeaders()),
    };
    if (extra) {
        Object.assign(headers, extra);
    }
    return headers;
}

/** Just the internal token and the session, for `http.request()` options. */
export async function internalHeaders(): Promise<Record<string, string>> {
    const headers: Record<string, string> = {};
    const token = getInternalToken();
    if (token) headers['X-Synapse-Internal'] = token;
    const session = await sessionToken();
    if (session) headers['X-Synapse-Session'] = session;
    return headers;
}

export { BACKEND_URL, getInternalToken };
