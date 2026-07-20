/**
 * POST /api/auth/login
 * Proxies to backend, sets the synapse_session HttpOnly cookie on success.
 */
import { NextRequest, NextResponse } from 'next/server';
import { BACKEND_URL, backendHeaders } from '@/lib/backend';

export const dynamic = 'force-dynamic';

export async function POST(req: NextRequest) {
    try {
        const body = await req.json();

        const backendRes = await fetch(`${BACKEND_URL}/api/auth/login`, {
            method: 'POST',
            headers: backendHeaders(),
            body: JSON.stringify(body),
        });

        if (!backendRes.ok) {
            const err = await backendRes.json().catch(() => ({ detail: 'Login failed' }));
            return NextResponse.json(
                { success: false, error: err.detail || 'Invalid credentials' },
                { status: backendRes.status }
            );
        }

        const data = await backendRes.json();
        const response = NextResponse.json({ success: true });

        if (data.token) {
            // Set `Secure` based on the actual request protocol, not NODE_ENV.
            // Self-hosted HTTP/LAN installs run NODE_ENV=production but over plain
            // http, where a Secure cookie would be dropped (→ login loop). A
            // TLS-terminating reverse proxy forwards the real scheme via header.
            const isHttps =
                req.nextUrl.protocol === 'https:' ||
                req.headers.get('x-forwarded-proto') === 'https';
            response.cookies.set('synapse_session', data.token, {
                httpOnly: true,
                secure: isHttps,
                sameSite: 'lax',
                maxAge: 60 * 60 * 24 * 7,
                path: '/',
            });
        }
        // Clear auth cache so middleware re-evaluates on next request
        response.cookies.set('synapse_auth_cache', '', { maxAge: 0, path: '/' });

        return response;
    } catch (err: any) {
        return NextResponse.json(
            { success: false, error: `Proxy error: ${err.message}` },
            { status: 500 }
        );
    }
}
