/**
 * How Home reads what it shows (F1612): every read gives up after a while, and a read still pending is never asked
 * again. Self-contained (no imports) so a test runs it in node.
 */

/**
 * How long a Home read waits before Home says it could not load it. Home's reads are reads of the local store, which
 * answer in well under a second; 15 s outlasts a cold backend start or a busy store writer by several seconds, and
 * past it a person has looked at an empty section long enough. The proxy's own limit (610 s, backendProxy.ts) is for
 * long model calls, not a page's reads, and the app's other timeouts are a health probe's (2 s) or a model's (60 s+).
 */
export const HOME_READ_TIMEOUT_MS = 15_000;

type Fetch = (input: string, init?: { signal?: AbortSignal }) => Promise<{ ok: boolean; status?: number; json(): Promise<unknown> }>;

/** `url`'s JSON, or a rejection when it answers with an error or has not answered within `timeoutMs`. */
export function readJson<T = unknown>(
    url: string,
    { timeoutMs = HOME_READ_TIMEOUT_MS, fetchImpl }: { timeoutMs?: number; fetchImpl?: Fetch } = {},
): Promise<T> {
    const get: Fetch = fetchImpl ?? ((input, init) => fetch(input, init));
    return get(url, { signal: AbortSignal.timeout(timeoutMs) })
        .then(r => (r.ok ? (r.json() as Promise<T>) : Promise.reject(new Error(`${url}: ${r.status ?? 'error'}`))));
}

/**
 * `read` as a function that is not asked again while its last call is pending: it returns that call's promise the
 * first time and `null` until it settles. A poll of a stalled backend holds one request, not one every 10 s.
 */
export function oneAtATime<T>(read: () => Promise<T>): () => Promise<T> | null {
    let pending: Promise<T> | null = null;
    return () => {
        if (pending) return null;
        const call = read();
        pending = call;
        call.then(() => { pending = null; }, () => { pending = null; });
        return call;
    };
}
