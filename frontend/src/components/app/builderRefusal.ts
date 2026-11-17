/**
 * What the Synapse AI panel says when the builder answers a send with an error (F1666).
 *
 * A refusal that says why (a 4xx whose body holds the reason in words, such as an image sent to a builder model that
 * cannot see images) is said in its own words, so the person reads what to do. This edition's proxy
 * (`app/api/builder/{chat,resume}/route.ts`) wraps the backend's body as `{"error": "Backend Error 422: <body>"}`; a
 * host that passes the body through gives `{"detail": "<reason>"}`. Both are read. Anything else (a 5xx, a field list,
 * a body that is not JSON, an empty reason) is "The builder could not be reached (<status>)." as before.
 */
export async function builderRefusal(response: Response): Promise<string> {
    const reached = `The builder could not be reached (${response.status}).`;
    if (response.status < 400 || response.status >= 500) return reached;
    let body: unknown;
    try {
        body = JSON.parse(await response.text());
    } catch {
        return reached;
    }
    return reasonIn(body) ?? reached;
}

/** The reason in words a refusal's body holds, directly or inside the proxy's wrapping, or null. */
function reasonIn(body: unknown): string | null {
    if (!body || typeof body !== 'object') return null;
    const { detail, error } = body as { detail?: unknown; error?: unknown };
    if (typeof detail === 'string' && detail.trim()) return detail.trim();
    if (typeof error === 'string') {
        const wrapped = error.match(/^Backend Error \d{3}: ([\s\S]*)$/);
        if (wrapped) {
            try {
                return reasonIn(JSON.parse(wrapped[1]));
            } catch {
                return null;
            }
        }
    }
    return null;
}
