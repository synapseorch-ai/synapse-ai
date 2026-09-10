import { NextResponse } from 'next/server';

import { BACKEND_URL, backendHeaders } from '@/lib/backend';

export const dynamic = 'force-dynamic';

/** Forwards the visitor's token; the backend is the only thing that reads it. */
export async function POST(req: Request) {
  try {
    const response = await fetch(`${BACKEND_URL}/api/widget/cancel`, {
      method: 'POST',
      headers: backendHeaders({ Authorization: req.headers.get('authorization') ?? '' }),
    });
    return NextResponse.json(await response.json().catch(() => ({})), {
      status: response.status,
    });
  } catch {
    return NextResponse.json({ detail: 'Proxy error.' }, { status: 500 });
  }
}
