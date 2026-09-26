import { NextResponse } from 'next/server';
import * as http from 'http';
import { Readable } from 'stream';
import { URL } from 'url';
import { internalHeaders } from '@/lib/backend';

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

    // Read here, in the request's scope: the executor below cannot await.
    const auth = await internalHeaders();
    const upstream = await new Promise<{ stream: ReadableStream; status: number; contentType: string }>((resolve, reject) => {
        const options: http.RequestOptions = {
            hostname: BACKEND_HOST,
            port: BACKEND_PORT,
            path: `/api/orchestrations/runs/${run_id}/human-input`,
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Content-Length': Buffer.byteLength(postData),
                'Accept-Encoding': 'identity',
                ...auth,
            },
        };

        const proxyReq = http.request(options, (proxyRes) => {
            resolve({
                stream: Readable.toWeb(proxyRes) as ReadableStream,
                status: proxyRes.statusCode || 200,
                contentType: String(proxyRes.headers['content-type'] || 'application/json'),
            });
        });

        proxyReq.on('error', reject);
        proxyReq.write(postData);
        proxyReq.end();
    });

    // A refusal is the backend's own answer, status and body (F215): the
    // losing answer of a race gets its 409, an unknown run its 404. Relabelled
    // as a 200 event stream, the loser was told its answer had applied.
    if (upstream.status < 200 || upstream.status >= 300) {
        return new NextResponse(upstream.stream, {
            status: upstream.status,
            headers: { 'Content-Type': upstream.contentType },
        });
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
