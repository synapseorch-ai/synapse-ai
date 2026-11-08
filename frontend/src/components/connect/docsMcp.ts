/**
 * Whether the docs' MCP server answers (GUIDE1, F1030): Home's "Learn Synapse with your AI assistant" shows only when
 * it does, so no workspace meets a dead link before DOC5 publishes the docs. Asked on the server (the docs site sends
 * no CORS headers, so a browser can't read the answer), cached for an hour; no answer reads as not yet.
 *
 * Its SDK answers a plain GET with 405 (POST is the protocol), so 405 is "here"; 404 is "not published yet".
 */
import { DOCS_MCP_URL } from './clients';

export async function docsMcpAnswers(): Promise<boolean> {
    try {
        const res = await fetch(DOCS_MCP_URL, { method: 'GET', next: { revalidate: 3600 }, signal: AbortSignal.timeout(2500) });
        return res.status === 405 || res.status === 200 || res.status === 406;
    } catch {
        return false;
    }
}
