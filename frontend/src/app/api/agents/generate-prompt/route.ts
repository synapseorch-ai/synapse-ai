import { NextResponse } from 'next/server';
import { BACKEND_URL, backendHeaders } from '@/lib/backend';

export const dynamic = 'force-dynamic';

export const maxDuration = 300; // 5 minutes timeout for LLM prompt generation

export async function POST(req: Request) {
    try {
        const body = await req.json();

        const backendResponse = await fetch(`${BACKEND_URL}/api/agents/generate-prompt`, {
            method: 'POST',
            headers: backendHeaders(),
            body: JSON.stringify(body),
        });

        if (!backendResponse.ok) {
            const text = await backendResponse.text();
            return NextResponse.json({ error: `Backend Error ${backendResponse.status}: ${text}` }, { status: backendResponse.status });
        }

        const data = await backendResponse.json();
        return NextResponse.json(data);

    } catch (error: any) {
        console.error("Generate prompt proxy error:", error);
        return NextResponse.json({ error: `Proxy Error: ${error.message}` }, { status: 500 });
    }
}
