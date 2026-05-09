import { NextResponse } from 'next/server';
import * as http from 'http';
import { URL } from 'url';
import { internalTokenHeader } from '@/lib/backend';

const _backendUrl = new URL(process.env.BACKEND_URL || 'http://127.0.0.1:8765');
const BACKEND_HOST = _backendUrl.hostname;
const BACKEND_PORT = parseInt(_backendUrl.port || '8765', 10);

export const maxDuration = 600;
export const dynamic = 'force-dynamic';

export async function POST(
    req: Request,
    { params }: { params: Promise<{ run_id: string }> }
) {
    const { run_id } = await params;
    const body = await req.json();
    const postData = JSON.stringify(body);

    // Use Node.js native http instead of fetch (undici) — undici buffers the
    // entire response before exposing body, which defeats SSE streaming.
    const webStream = await new Promise<ReadableStream>((resolve, reject) => {
        const options: http.RequestOptions = {
            hostname: BACKEND_HOST,
            port: BACKEND_PORT,
            path: `/api/orchestrations/runs/${run_id}/resume`,
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Content-Length': Buffer.byteLength(postData),
                'Accept-Encoding': 'identity',
                ...internalTokenHeader(),
            },
        };

        const proxyReq = http.request(options, (proxyRes) => {
            const webStream = new ReadableStream({
                start(controller) {
                    proxyRes.on('data', (chunk: Buffer) => controller.enqueue(chunk));
                    proxyRes.on('end', () => controller.close());
                    proxyRes.on('error', (err) => controller.error(err));
                },
            });
            resolve(webStream);
        });

        proxyReq.on('error', reject);
        proxyReq.write(postData);
        proxyReq.end();
    });

    return new NextResponse(webStream, {
        headers: {
            'Content-Type': 'text/event-stream',
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            'X-Accel-Buffering': 'no',
        },
    });
}
