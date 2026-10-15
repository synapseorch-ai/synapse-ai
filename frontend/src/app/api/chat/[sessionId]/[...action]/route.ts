import { NextResponse } from 'next/server';
import { BACKEND_URL, sessionHeaders } from '@/lib/backend';

export const dynamic = 'force-dynamic';

/**
 * One conversation's small actions, sent on to the backend's `/chat/{sid}/…` (F621).
 *
 * The backend serves a conversation's routes without `/api` (`/chat/{sid}/helpers/{id}/stop`,
 * `/members/{id}/stop`, `/members/{id}/message`, `/lead/message`), and the catch-all proxy keeps
 * `/api` in the path, so every one of them it forwarded was a 404: a helper's Stop did nothing.
 * `approve`, `cancel` and `events` keep their own handlers (they stream, or answer at once), which
 * win over this one.
 */
export async function POST(
    req: Request,
    { params }: { params: Promise<{ sessionId: string; action: string[] }> },
) {
    const { sessionId, action } = await params;
    const path = [sessionId, ...action].map(encodeURIComponent).join('/');
    try {
        const body = await req.text();
        const res = await fetch(`${BACKEND_URL}/chat/${path}`, {
            method: 'POST',
            headers: await sessionHeaders(),
            body: body || undefined,
        });
        return new NextResponse(await res.text(), {
            status: res.status,
            headers: { 'Content-Type': res.headers.get('Content-Type') || 'application/json' },
        });
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    } catch (error: any) {
        return NextResponse.json({ error: `Proxy Error: ${error.message}` }, { status: 500 });
    }
}
