import { NextResponse } from 'next/server';
import { BACKEND_URL, sessionHeaders } from '@/lib/backend';

export const dynamic = 'force-dynamic';

export async function GET(request: Request) {
    try {
        // `?refresh=1` asks the model CLIs again (CLIM1).
        const refresh = new URL(request.url).searchParams.get('refresh') ? '?refresh=1' : '';
        const res = await fetch(`${BACKEND_URL}/api/models${refresh}`, {
            headers: await sessionHeaders(),
        });
        if (!res.ok) {
            return NextResponse.json({ providers: {} }, { status: res.status });
        }
        const data = await res.json();
        return NextResponse.json(data);
    } catch (error: unknown) {
        const message = error instanceof Error ? error.message : 'Unknown error';
        console.error('models proxy error:', message);
        return NextResponse.json({ providers: {} }, { status: 500 });
    }
}
