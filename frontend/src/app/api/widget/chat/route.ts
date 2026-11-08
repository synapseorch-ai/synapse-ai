import { NextResponse } from 'next/server';

import { BACKEND_URL, sessionHeaders } from '@/lib/backend';

export const dynamic = 'force-dynamic';
export const maxDuration = 300;

/**
 * The visitor's turn, streamed.
 *
 * Two credentials go to the backend and both are required: this proxy's internal
 * token, which says the request came through our own app, and the visitor's
 * `Authorization` header, which says which widget, agent and conversation. The
 * second is forwarded verbatim and never inspected here — the backend holds the
 * signing key, and a proxy that made its own judgement about a token would be a
 * second place to get that judgement wrong.
 *
 * `signal` is forwarded for the same reason `/api/chat/stream` forwards it: a
 * closed tab must not leave this proxy holding a request nobody reads. It closes
 * the reader, not the turn — a visitor clicking through the host site reloads
 * this frame, and the frame reads the turn again from `/api/widget/stream`
 * (F49). Stop is `/api/widget/cancel`.
 */
export async function POST(req: Request) {
  try {
    const body = await req.text();
    const response = await fetch(`${BACKEND_URL}/api/widget/chat`, {
      method: 'POST',
      headers: await sessionHeaders({
        Authorization: req.headers.get('authorization') ?? '',
        'X-Forwarded-For': req.headers.get('x-forwarded-for') ?? '',
      }),
      body,
      signal: req.signal,
    });

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
