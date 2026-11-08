/**
 * Every /api and /auth request no route handler of ours answers, sent on to the
 * backend (F309).
 *
 * This was a fallback rewrite in `next.config.ts`. A rewrite's destination is
 * written into the build's routes manifest, so a packaged UI proxied to the
 * backend port it was built for and nowhere else: started beside a backend on
 * another port, every call answered 500 (ECONNREFUSED). Here the address is the
 * one this server was started with (`BACKEND_URL`, `lib/backend.ts`).
 *
 * It does what the rewrite did, and nothing more:
 * - headers go on as they arrived. `proxy.ts` has already put the internal
 *   token and the session on them where it runs. This adds neither itself: its
 *   matcher skips static-file paths, and a token added here would let such a
 *   path through without the login gate. As the rewrite did, the backend sees
 *   its own `Host` and the browser's in `X-Forwarded-Host`;
 * - bodies stream both ways, so an upload is never held in memory and an event
 *   stream is never buffered (the rewrite's undici did buffer them, which is
 *   why the SSE routes have their own handlers);
 * - a backend silent for 610 s is given up on (F169's `proxyTimeout`): past
 *   the longest bounded wait, a synchronous v1 chat at 600 s;
 * - redirects and cookies pass through untouched, and a client that goes away
 *   ends the backend's request.
 *
 * Over `node:http`, as the SSE routes are, rather than `fetch`: no body is
 * buffered and no encoding is undone on the way.
 */
import * as http from 'http';
import * as https from 'https';
import { Readable } from 'stream';

import { BACKEND_URL } from '@/lib/backend';

const IDLE_TIMEOUT_MS = 610_000;

// Hop-by-hop: they describe one connection, not the request.
const HOP_BY_HOP = new Set([
    'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
    'proxy-connection', 'te', 'trailer', 'transfer-encoding', 'upgrade',
]);

export async function proxyToBackend(req: Request): Promise<Response> {
    const backend = new URL(BACKEND_URL);
    const url = new URL(req.url);
    const headers: http.OutgoingHttpHeaders = {};
    req.headers.forEach((value, name) => {
        if (!HOP_BY_HOP.has(name)) headers[name] = value;
    });
    headers['x-forwarded-host'] = req.headers.get('host') ?? '';
    headers.host = backend.host;
    const hasBody = req.body !== null && req.method !== 'GET' && req.method !== 'HEAD';

    const transport = backend.protocol === 'https:' ? https : http;

    return new Promise<Response>((resolve) => {
        const upstream = transport.request({
            hostname: backend.hostname,
            port: backend.port || (backend.protocol === 'https:' ? 443 : 80),
            method: req.method,
            path: `${url.pathname}${url.search}`,
            headers,
        }, (res) => {
            const out = new Headers();
            for (const [name, value] of Object.entries(res.headers)) {
                if (value === undefined || HOP_BY_HOP.has(name)) continue;
                if (Array.isArray(value)) value.forEach((v) => out.append(name, v));
                else out.set(name, String(value));
            }
            const status = res.statusCode ?? 502;
            const empty = req.method === 'HEAD' || status === 204 || status === 304;
            if (empty) res.resume();
            resolve(new Response(empty ? null : (Readable.toWeb(res) as ReadableStream<Uint8Array>), {
                status,
                statusText: res.statusMessage,
                headers: out,
            }));
        });
        upstream.setTimeout(IDLE_TIMEOUT_MS, () => upstream.destroy(new Error('the backend was silent for 610 s')));
        upstream.on('error', (err) => {
            // What the rewrite answered when the backend could not be reached.
            if (!req.signal.aborted) console.error(`Failed to proxy ${backend.origin}${url.pathname}`, err);
            resolve(new Response('Internal Server Error', { status: 500 }));
        });
        req.signal.addEventListener('abort', () => upstream.destroy(), { once: true });
        if (hasBody) {
            Readable.fromWeb(req.body as unknown as import('stream/web').ReadableStream).pipe(upstream);
        } else {
            upstream.end();
        }
    });
}
