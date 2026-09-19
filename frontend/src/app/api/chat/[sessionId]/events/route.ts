import { NextResponse } from 'next/server';
import { BACKEND_URL, backendHeaders } from '@/lib/backend';

export const dynamic = 'force-dynamic';

export const maxDuration = 300;

/**
 * Read a conversation's turn again, from a cursor (F49).
 *
 * A turn runs on the server whether or not anyone is reading, so a page whose
 * stream broke — a reload, a laptop waking up — reads it again here rather than
 * sending the question again. `after` (or `Last-Event-ID`) is the cursor of the
 * last event the page handled. 404 and 410 pass through: they mean "nothing to
 * read — the answer is in the conversation", and the page acts on that.
 */
export async function GET(
    req: Request,
    { params }: { params: Promise<{ sessionId: string }> },
) {
    const { sessionId } = await params;
    const after = new URL(req.url).searchParams.get('after');
    const headers: Record<string, string> = { ...backendHeaders() };
    const lastEventId = req.headers.get('last-event-id');
    if (lastEventId) headers['Last-Event-ID'] = lastEventId;
    try {
        const backendResponse = await fetch(
            `${BACKEND_URL}/chat/${encodeURIComponent(sessionId)}/events${after ? `?after=${encodeURIComponent(after)}` : ''}`,
            // Aborting closes this reader only; the turn is not the reader's.
            { headers, signal: req.signal },
        );
        if (!backendResponse.ok) {
            return NextResponse.json(await backendResponse.json().catch(() => ({})),
                { status: backendResponse.status });
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
