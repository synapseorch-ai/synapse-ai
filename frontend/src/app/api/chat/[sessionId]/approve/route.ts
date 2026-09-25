import { NextResponse } from 'next/server';
import { BACKEND_URL, sessionHeaders } from '@/lib/backend';

export const dynamic = 'force-dynamic';
export const maxDuration = 300;

/**
 * Answer the tool-call question a conversation is waiting on.
 *
 * Streams, like `/chat/stream` does: the answer resumes the same turn rather
 * than starting a new one, so what comes back is the rest of that turn.
 */
export async function POST(
    req: Request,
    { params }: { params: Promise<{ sessionId: string }> },
) {
    const { sessionId } = await params;
    try {
        const body = await req.json();
        const backendResponse = await fetch(
            `${BACKEND_URL}/chat/${encodeURIComponent(sessionId)}/approve`,
            {
                method: 'POST',
                headers: await sessionHeaders(),
                body: JSON.stringify(body),
                signal: req.signal,
            },
        );

        if (!backendResponse.ok) {
            const text = await backendResponse.text();
            return NextResponse.json(
                { error: `Backend Error ${backendResponse.status}: ${text}` },
                { status: backendResponse.status },
            );
        }

        return new NextResponse(backendResponse.body, {
            headers: {
                'Content-Type': 'text/event-stream',
                'Cache-Control': 'no-cache',
                'Connection': 'keep-alive',
            },
        });
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    } catch (error: any) {
        return NextResponse.json({ error: `Proxy Error: ${error.message}` }, { status: 500 });
    }
}
