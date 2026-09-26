import { NextResponse } from 'next/server';
import { BACKEND_URL, sessionHeaders } from '@/lib/backend';

export const dynamic = 'force-dynamic';

export const maxDuration = 300; // 5 minutes timeout for LLM prompt generation

export async function POST(req: Request) {
    try {
        const body = await req.json();

        const backendResponse = await fetch(`${BACKEND_URL}/api/agents/generate-prompt`, {
            method: 'POST',
            headers: await sessionHeaders(),
            body: JSON.stringify(body),
        });

        if (!backendResponse.ok) {
            // The backend's own `{"detail": …}` is the sentence the editor shows (F183).
            const text = await backendResponse.text();
            let failure: unknown;
            try { failure = JSON.parse(text); } catch { failure = { detail: text || `Backend error ${backendResponse.status}` }; }
            return NextResponse.json(failure, { status: backendResponse.status });
        }

        const data = await backendResponse.json();
        return NextResponse.json(data);

    } catch (error: any) {
        console.error("Generate prompt proxy error:", error);
        return NextResponse.json({ detail: `The prompt writer could not reach the server: ${error.message}` }, { status: 502 });
    }
}
