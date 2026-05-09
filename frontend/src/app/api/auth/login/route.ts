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
            response.cookies.set('synapse_session', data.token, {
                httpOnly: true,
                secure: process.env.NODE_ENV === 'production',
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
