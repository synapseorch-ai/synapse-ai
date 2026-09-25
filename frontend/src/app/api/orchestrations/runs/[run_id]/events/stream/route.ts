import { NextResponse } from 'next/server';
import * as http from 'http';
import { URL } from 'url';
import { internalHeaders } from '@/lib/backend';

const _backendUrl = new URL(process.env.BACKEND_URL || 'http://127.0.0.1:8765');
const BACKEND_HOST = _backendUrl.hostname;
const BACKEND_PORT = parseInt(_backendUrl.port || '8765', 10);

export const maxDuration = 600;
export const dynamic = 'force-dynamic';

// Reattach stream: journal replay + live tail. Needs a dedicated raw-http
// proxy because the Next fallback rewrite (undici) buffers the whole response
// before exposing it, which defeats SSE — same reason as the POST run proxy.
export async function GET(
    req: Request,
    { params }: { params: Promise<{ run_id: string }> }
) {
    const { run_id } = await params;
    const reqUrl = new URL(req.url);
    const after = reqUrl.searchParams.get('after') || '0';
    const lastEventId = req.headers.get('last-event-id');

    // Read here, in the request's scope: the executor below cannot await.
    const auth = await internalHeaders();
    const upstream = await new Promise<{ stream: ReadableStream; status: number }>((resolve, reject) => {
        const options: http.RequestOptions = {
            hostname: BACKEND_HOST,
            port: BACKEND_PORT,
            path: `/api/orchestrations/runs/${encodeURIComponent(run_id)}/events/stream?after=${encodeURIComponent(after)}`,
            method: 'GET',
            headers: {
                'Accept-Encoding': 'identity',
                ...(lastEventId ? { 'Last-Event-ID': lastEventId } : {}),
                ...auth,
            },
        };

        const proxyReq = http.request(options, (proxyRes) => {
            const stream = new ReadableStream({
                start(controller) {
                    proxyRes.on('data', (chunk: Buffer) => controller.enqueue(chunk));
                    proxyRes.on('end', () => controller.close());
                    proxyRes.on('error', (err) => controller.error(err));
                },
                cancel() {
                    proxyReq.destroy();
                },
            });
            resolve({ stream, status: proxyRes.statusCode || 200 });
        });

        proxyReq.on('error', reject);
        proxyReq.end();
    });

    if (upstream.status !== 200) {
        return new NextResponse(upstream.stream, { status: upstream.status });
    }
    return new NextResponse(upstream.stream, {
        headers: {
            'Content-Type': 'text/event-stream',
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            'X-Accel-Buffering': 'no',
        },
    });
}
