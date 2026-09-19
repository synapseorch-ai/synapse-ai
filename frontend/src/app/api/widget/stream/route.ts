import { NextResponse } from 'next/server';

import { BACKEND_URL, backendHeaders } from '@/lib/backend';

export const dynamic = 'force-dynamic';
export const maxDuration = 300;

/**
 * The visitor's turn, read again from a cursor (F49).
 *
 * A turn outlives the frame that started it — a visitor clicking through the
 * host site reloads this frame — so a reloaded frame whose history says a turn
 * is running, and a frame whose stream broke, read it here. In the hosted
 * product a queued turn's events always arrive this way. 404/410 pass through:
 * nothing to read, the answer is in the history.
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
