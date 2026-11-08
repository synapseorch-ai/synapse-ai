import { NextResponse } from 'next/server';

import { BACKEND_URL, sessionHeaders } from '@/lib/backend';

export const dynamic = 'force-dynamic';

/**
 * A new conversation for the agent the widget answers with now (F278). Forwards
 * the visitor's old token — the backend checks it and the origin — and the
 * address, which counts against the same limit as a new session.
 */
export async function POST(req: Request) {
  try {
    const response = await fetch(`${BACKEND_URL}/api/widget/restart`, {
      method: 'POST',
      headers: await sessionHeaders({
        Authorization: req.headers.get('authorization') ?? '',
        'X-Forwarded-For': req.headers.get('x-forwarded-for') ?? '',
      }),
    });
    return NextResponse.json(await response.json().catch(() => ({})), {
      status: response.status,
    });
  } catch {
    return NextResponse.json({ detail: 'Proxy error.' }, { status: 500 });
  }
}
