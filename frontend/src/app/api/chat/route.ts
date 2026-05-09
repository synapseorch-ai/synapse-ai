import { NextResponse } from 'next/server';
import { BACKEND_URL, backendHeaders } from '@/lib/backend';

export const dynamic = 'force-dynamic';

export const maxDuration = 300; // 5 minutes timeout (Vercel/Next.js specific)

export async function POST(req: Request) {
    try {
        const body = await req.json();

        // Forward request to Python Backend
        // Since this runs on the server, localhost:8000 refers to the container's localhost
        const backendResponse = await fetch(`${BACKEND_URL}/chat`, {
            method: 'POST',
            headers: backendHeaders(),
            body: JSON.stringify(body),
            // No manual abort signal -> Node.js default timeout (long)
        });

        if (!backendResponse.ok) {
            const text = await backendResponse.text();
            return NextResponse.json({ error: `Backend Error ${backendResponse.status}: ${text}` }, { status: backendResponse.status });
        }

        const data = await backendResponse.json();
        return NextResponse.json(data);

    } catch (error: any) {
        console.error("Proxy Error:", error);
        return NextResponse.json({ error: `Proxy Error: ${error.message}` }, { status: 500 });
    }
}
