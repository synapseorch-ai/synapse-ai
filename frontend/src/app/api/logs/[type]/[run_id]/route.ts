import { NextResponse } from 'next/server';
import { BACKEND_URL, backendHeaders } from '@/lib/backend';

export const dynamic = 'force-dynamic';

export async function GET(
    _req: Request,
    { params }: { params: Promise<{ type: string; run_id: string }> }
) {
    const { type, run_id } = await params;
    try {
        const res = await fetch(`${BACKEND_URL}/api/logs/${type}/${run_id}`, {
            cache: 'no-store',
            headers: backendHeaders(),
        });
        if (!res.ok) return NextResponse.json({ error: 'Not found' }, { status: res.status });
        const text = await res.text();
        return new NextResponse(text, {
            status: 200,
            headers: { 'Content-Type': 'text/plain; charset=utf-8' },
        });
    } catch (error: any) {
        return NextResponse.json({ error: error.message }, { status: 500 });
    }
}

export async function DELETE(
    _req: Request,
    { params }: { params: Promise<{ type: string; run_id: string }> }
) {
    const { type, run_id } = await params;
    try {
        const res = await fetch(`${BACKEND_URL}/api/logs/${type}/${run_id}`, {
            method: 'DELETE',
            cache: 'no-store',
            headers: backendHeaders(),
        });
        const data = await res.json();
        return NextResponse.json(data, { status: res.status });
    } catch (error: any) {
        return NextResponse.json({ error: error.message }, { status: 500 });
    }
}
