import { NextResponse } from 'next/server';
import { BACKEND_URL, backendHeaders } from '@/lib/backend';

export const dynamic = 'force-dynamic';

export async function GET() {
    try {
        const res = await fetch(`${BACKEND_URL}/api/agent-types`, {
            headers: backendHeaders(),
        });
        if (!res.ok) {
            return NextResponse.json({ types: [] }, { status: res.status });
        }
        const data = await res.json();
        return NextResponse.json(data);
    } catch (error: unknown) {
        const message = error instanceof Error ? error.message : 'Unknown error';
        console.error('agent-types proxy error:', message);
        return NextResponse.json({ types: [] }, { status: 500 });
    }
}
