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
    { params }: { params: Promise<{ orch_id: string }> }
) {
    const { orch_id } = await params;
    console.log(`[route] POST /api/orchestrations/${orch_id}/run hit`);
    const body = await req.json();
    const postData = JSON.stringify(body);

    // Use Node.js native http instead of fetch (undici) — undici buffers the
    // entire response before exposing body, which defeats SSE streaming.
    // Native http.request fires 'data' events per-chunk in real time.
    const webStream = await new Promise<ReadableStream>((resolve, reject) => {
        const options: http.RequestOptions = {
            hostname: BACKEND_HOST,
            port: BACKEND_PORT,
            path: `/api/orchestrations/${orch_id}/run`,
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Content-Length': Buffer.byteLength(postData),
                'Accept-Encoding': 'identity',
                ...internalTokenHeader(),
            },
        };

        const proxyReq = http.request(options, (proxyRes) => {
            console.log('[stream] backend connected, status:', proxyRes.statusCode);
            let chunkCount = 0;
            const startTime = Date.now();

            const webStream = new ReadableStream({
                start(controller) {
                    proxyRes.on('data', (chunk: Buffer) => {
                        chunkCount++;
                        console.log(`[stream] chunk #${chunkCount} at +${Date.now() - startTime}ms (${chunk.length} bytes)`);
                        controller.enqueue(chunk);
                    });
                    proxyRes.on('end', () => {
                        console.log(`[stream] done — ${chunkCount} chunks in ${Date.now() - startTime}ms`);
                        controller.close();
                    });
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
