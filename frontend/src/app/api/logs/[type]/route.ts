import { NextResponse } from 'next/server';
import { BACKEND_URL, sessionHeaders } from '@/lib/backend';

export const dynamic = 'force-dynamic';

export async function GET(
    req: Request,
    { params }: { params: Promise<{ type: string }> }
) {
    const { type } = await params;
    // Forward the query string. Without this the backend saw no `limit`/`offset`
    // and answered with its defaults, so the Logs screen's "load more" re-fetched
    // the same first page — and `?agent_id=` (the agent editor's Runs tab) would
    // have listed every agent's runs under whichever agent was open.
    const query = new URL(req.url).search;
    try {
        const res = await fetch(`${BACKEND_URL}/api/logs/${type}${query}`, {
            cache: 'no-store',
            headers: await sessionHeaders(),
        });
        const data = await res.json();
        return NextResponse.json(data, { status: res.status });
    } catch (error: any) {
        return NextResponse.json({ error: error.message }, { status: 500 });
    }
}
