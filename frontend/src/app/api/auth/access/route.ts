/**
 * The access link (login off, D124): swap this install's access token for a
 * session cookie.
 *
 * GET  /api/auth/access?token=…&redirect=/path   — the link, via proxy.ts
 * POST /api/auth/access {token}                  — the login page's paste box
 *
 * The backend decides (`POST /api/auth/access`): the token must be the
 * install's, and a password always wins — with login on it answers 409.
 */
import { NextRequest, NextResponse } from 'next/server';
import { BACKEND_URL, sessionHeaders } from '@/lib/backend';

export const dynamic = 'force-dynamic';

const WEEK = 60 * 60 * 24 * 7;

async function exchange(token: string): Promise<{ ok: boolean; status: number; session?: string; error?: string }> {
    const res = await fetch(`${BACKEND_URL}/api/auth/access`, {
        method: 'POST',
        headers: await sessionHeaders(),
        body: JSON.stringify({ token }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) return { ok: false, status: res.status, error: data.detail || 'That link did not work.' };
    return { ok: true, status: 200, session: data.token };
}

function withSession(response: NextResponse, req: NextRequest, session: string): NextResponse {
    // `Secure` from the real scheme, as /api/auth/login does: a plain-http LAN
    // install would drop a Secure cookie and loop back to the gate.
    const isHttps = req.nextUrl.protocol === 'https:' || req.headers.get('x-forwarded-proto') === 'https';
    response.cookies.set('synapse_session', session, {
        httpOnly: true,
        secure: isHttps,
        sameSite: 'lax',
        maxAge: WEEK,
        path: '/',
    });
    return response;
}

/**
 * A redirect to a path on the host the visitor used. Relative, on purpose: in
 * the standalone server `req.url` carries the bind address (`0.0.0.0:3000`),
 * so an absolute URL built from it sent the browser to a host its new cookie
 * was not set for — straight back to the gate.
 */
function redirectTo(path: string): NextResponse {
    return new NextResponse(null, { status: 303, headers: { Location: path } });
}

/** Only same-origin paths — `//evil.example` would make this an open redirect. */
function safePath(requested: string | null): string {
    return requested && requested.startsWith('/') && !requested.startsWith('//') ? requested : '/';
}

export async function GET(req: NextRequest) {
    const token = req.nextUrl.searchParams.get('token') ?? '';
    const next = safePath(req.nextUrl.searchParams.get('redirect'));
    const result = await exchange(token).catch(() => ({ ok: false, status: 503 } as const));
    if (!result.ok || !('session' in result) || !result.session) {
        const why = result.status === 409 ? 'login-on' : 'invalid';
        return redirectTo(`/login?redirect=${encodeURIComponent(next)}&link=${why}`);
    }
    return withSession(redirectTo(next), req, result.session);
}

export async function POST(req: NextRequest) {
    const body = await req.json().catch(() => ({}));
    const token = typeof body.token === 'string' ? body.token.trim() : '';
    // A pasted whole link works too: take the `token` out of it.
    const fromLink = (() => {
        try {
            return new URL(token).searchParams.get('token') ?? token;
        } catch {
            return token;
        }
    })();
    const result = await exchange(fromLink).catch(() => ({ ok: false, status: 503, error: 'Synapse is not answering.' }));
    if (!result.ok || !('session' in result) || !result.session) {
        return NextResponse.json({ success: false, error: 'error' in result ? result.error : undefined }, { status: result.status });
    }
    return withSession(NextResponse.json({ success: true }), req, result.session);
}
