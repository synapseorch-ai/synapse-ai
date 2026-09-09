import { NextResponse } from 'next/server';
import { BACKEND_URL, backendHeaders } from '@/lib/backend';

export const dynamic = 'force-dynamic';

export const maxDuration = 300; // 5 minutes timeout for streaming

export async function POST(req: Request) {
    try {
        const body = await req.json();

        // Forward request to Python Backend SSE endpoint.
        //
        // `signal` matters: without it, a browser that gives up — a closed tab,
        // a navigation, the Stop button — left this proxy holding an open
        // request to a backend still generating an answer nobody would read.
        // The abort now reaches the backend, whose handler cancels the turn it
        // is streaming.
        const backendResponse = await fetch(`${BACKEND_URL}/chat/stream`, {
            method: 'POST',
            headers: backendHeaders(),
            body: JSON.stringify(body),
            signal: req.signal,
        });

        if (!backendResponse.ok) {
            const text = await backendResponse.text();
            return NextResponse.json(
                { error: `Backend Error ${backendResponse.status}: ${text}` }, 
                { status: backendResponse.status }
            );
        }

        // Stream the response from backend to frontend
        // This passes through the SSE stream
        return new NextResponse(backendResponse.body, {
            headers: {
                'Content-Type': 'text/event-stream',
                'Cache-Control': 'no-cache',
                'Connection': 'keep-alive',
            },
        });

    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    } catch (error: any) {
        console.error("SSE Proxy Error:", error);
        return NextResponse.json({ error: `Proxy Error: ${error.message}` }, { status: 500 });
    }
}
