import { NextResponse } from 'next/server';
import { BACKEND_URL, backendHeaders } from '@/lib/backend';

export const dynamic = 'force-dynamic';

/**
 * Stop the turn running for this chat session.
 *
 * Separate from aborting the stream, and both are needed. Aborting tells this
 * process to stop listening; this tells the backend to stop working. Without it
 * a stopped turn goes on burning tokens against a socket nobody is reading.
 */
export async function POST(
    _req: Request,
    { params }: { params: Promise<{ sessionId: string }> },
) {
    const { sessionId } = await params;
    try {
        const res = await fetch(
            `${BACKEND_URL}/chat/${encodeURIComponent(sessionId)}/cancel`,
            { method: 'POST', headers: backendHeaders() },
        );
        return NextResponse.json(await res.json(), { status: res.status });
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    } catch (error: any) {
        return NextResponse.json({ error: `Proxy Error: ${error.message}` }, { status: 500 });
    }
}
