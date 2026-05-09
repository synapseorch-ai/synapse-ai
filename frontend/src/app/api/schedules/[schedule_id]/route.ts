import { NextResponse } from 'next/server';
import { BACKEND_URL, backendHeaders } from '@/lib/backend';

export const dynamic = 'force-dynamic';

export async function GET(
    _req: Request,
    { params }: { params: Promise<{ schedule_id: string }> }
) {
    const { schedule_id } = await params;
    try {
        const res = await fetch(`${BACKEND_URL}/api/schedules/${schedule_id}`, {
            cache: 'no-store',
            headers: backendHeaders(),
        });
        const data = await res.json();
        return NextResponse.json(data, { status: res.status });
    } catch (error: unknown) {
        const message = error instanceof Error ? error.message : 'Unknown error';
        return NextResponse.json({ error: message }, { status: 500 });
    }
}

export async function PUT(
    req: Request,
    { params }: { params: Promise<{ schedule_id: string }> }
) {
    const { schedule_id } = await params;
    try {
        const body = await req.json();
        const res = await fetch(`${BACKEND_URL}/api/schedules/${schedule_id}`, {
            method: 'PUT',
            headers: backendHeaders(),
            body: JSON.stringify(body),
            cache: 'no-store',
        });
        const data = await res.json();
        return NextResponse.json(data, { status: res.status });
    } catch (error: unknown) {
        const message = error instanceof Error ? error.message : 'Unknown error';
        return NextResponse.json({ error: message }, { status: 500 });
    }
}

export async function PATCH(
    req: Request,
    { params }: { params: Promise<{ schedule_id: string }> }
) {
    const { schedule_id } = await params;
    try {
        const body = await req.json();
        const res = await fetch(`${BACKEND_URL}/api/schedules/${schedule_id}`, {
            method: 'PATCH',
            headers: backendHeaders(),
            body: JSON.stringify(body),
            cache: 'no-store',
        });
        const data = await res.json();
        return NextResponse.json(data, { status: res.status });
    } catch (error: unknown) {
        const message = error instanceof Error ? error.message : 'Unknown error';
        return NextResponse.json({ error: message }, { status: 500 });
    }
}

export async function DELETE(
    _req: Request,
    { params }: { params: Promise<{ schedule_id: string }> }
) {
    const { schedule_id } = await params;
    try {
        const res = await fetch(`${BACKEND_URL}/api/schedules/${schedule_id}`, {
            method: 'DELETE',
            cache: 'no-store',
            headers: backendHeaders(),
        });
        const data = await res.json();
        return NextResponse.json(data, { status: res.status });
    } catch (error: unknown) {
        const message = error instanceof Error ? error.message : 'Unknown error';
        return NextResponse.json({ error: message }, { status: 500 });
    }
}
