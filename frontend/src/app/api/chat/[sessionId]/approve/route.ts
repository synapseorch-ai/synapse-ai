import { NextResponse } from 'next/server';
import { BACKEND_URL, sessionHeaders } from '@/lib/backend';

export const dynamic = 'force-dynamic';
export const maxDuration = 300;

/**
 * Answer the tool-call question a conversation is waiting on.
 *
 * Streams, like `/chat/stream` does: the answer resumes the same turn rather
 * than starting a new one, so what comes back is the rest of that turn. A team
 * member's question answered while its turn still runs is a JSON reply instead
 * (TEAM1c): that turn's own stream shows the rest.
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

        // A team member's answer while its turn runs is a JSON reply (TEAM1c); a resume streams.
        if ((backendResponse.headers.get('content-type') || '').includes('application/json')) {
            return NextResponse.json(await backendResponse.json());
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
