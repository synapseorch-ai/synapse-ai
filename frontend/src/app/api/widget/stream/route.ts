import { NextResponse } from 'next/server';

import { BACKEND_URL, backendHeaders } from '@/lib/backend';

export const dynamic = 'force-dynamic';
export const maxDuration = 300;

/**
 * Present for parity with the hosted product, where a queued turn's events
 * arrive on a stream opened separately from the POST that started it.
 *
 * Self-hosted answers the POST with the stream itself, so the widget client
 * normally never reaches here — it branches on the content type it was handed.
 * This exists so that branch cannot 404 on a self-hosted deployment running its
 * chat through the queue, which `core/routes/chat.py` already supports.
 */
export async function GET(req: Request) {
  const cursor = new URL(req.url).searchParams.get('cursor') ?? '0';
  try {
    const response = await fetch(
      `${BACKEND_URL}/api/widget/stream?cursor=${encodeURIComponent(cursor)}`,
      {
        headers: backendHeaders({ Authorization: req.headers.get('authorization') ?? '' }),
        signal: req.signal,
      },
    );
    if (!response.ok) {
      return NextResponse.json(await response.json().catch(() => ({})), {
        status: response.status,
      });
    }
    return new NextResponse(response.body, {
      headers: {
        'Content-Type': 'text/event-stream',
        'Cache-Control': 'no-cache',
        Connection: 'keep-alive',
      },
    });
  } catch {
    return NextResponse.json({ detail: 'Proxy error.' }, { status: 500 });
  }
}
