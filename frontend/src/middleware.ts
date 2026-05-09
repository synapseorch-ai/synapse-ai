/**
 * Next.js Edge Middleware
 * -----------------------
 * 1. Injects X-Synapse-Internal header for backend-proxied routes.
 * 2. Enforces login gate: redirects unauthenticated users to /login
 *    when login_enabled is configured in Synapse settings.
 *
 * Auth flow (per request):
 *   a. Check `synapse_session` cookie → verify JWT locally (no network call)
 *   b. Check `synapse_auth_cache` cookie (60s TTL) → skip backend call if login is disabled
 *   c. Fetch /api/auth/status from backend (server-side, internal token injected)
 *   d. If login required → redirect to /login?redirect=<original_path>
 *   e. If login not required → cache result for 60s and proceed
 */
import { NextRequest, NextResponse } from 'next/server';
import { jwtVerify } from 'jose';

const AUTH_BYPASS_PREFIXES = [
    '/login',
    '/api/auth/',
    '/api/v1/',
    '/auth/',
    '/_next/',
    '/favicon',
];

function shouldBypassAuth(pathname: string): boolean {
    return AUTH_BYPASS_PREFIXES.some(p => pathname.startsWith(p));
}

async function verifyJwt(token: string, secret: string): Promise<boolean> {
    try {
        await jwtVerify(token, new TextEncoder().encode(secret), {
            algorithms: ['HS256'],
            issuer: 'synapse',
        });
        return true;
    } catch {
        return false;
    }
}

export async function middleware(request: NextRequest) {
    const { pathname } = request.nextUrl;
    const internalToken = process.env.SYNAPSE_INTERNAL_TOKEN || '';

    // Always inject the internal token
    const requestHeaders = new Headers(request.headers);
    if (internalToken) {
        requestHeaders.set('X-Synapse-Internal', internalToken);
    }

    // Bypass auth check for login page, auth API, external API, and static assets
    if (shouldBypassAuth(pathname)) {
        return NextResponse.next({ request: { headers: requestHeaders } });
    }

    const jwtSecret = process.env.SYNAPSE_JWT_SECRET || '';

    // Fast path: valid session cookie → proceed without hitting backend
    const sessionCookie = request.cookies.get('synapse_session')?.value;
    if (sessionCookie && jwtSecret) {
        const valid = await verifyJwt(sessionCookie, jwtSecret);
        if (valid) {
            return NextResponse.next({ request: { headers: requestHeaders } });
        }
    }

    // Cache hit: we already know login is not required (60s TTL)
    const authCache = request.cookies.get('synapse_auth_cache')?.value;
    if (authCache === 'no_auth_required') {
        return NextResponse.next({ request: { headers: requestHeaders } });
    }

    // Cache miss: ask the backend whether login is enabled
    const backendUrl = process.env.BACKEND_URL || 'http://127.0.0.1:8765';
    let loginRequired = false;
    try {
        const statusRes = await fetch(`${backendUrl}/api/auth/status`, {
            headers: {
                'X-Synapse-Internal': internalToken,
                'Content-Type': 'application/json',
            },
            signal: AbortSignal.timeout(2000),
        });
        if (statusRes.ok) {
            const status = await statusRes.json();
            loginRequired = status.login_enabled === true && status.login_configured === true;
        }
    } catch {
        // Backend unreachable — fail open so we don't lock users out
        loginRequired = false;
    }

    if (!loginRequired) {
        // Cache the "no auth" result for 60 seconds to avoid hitting backend on every request
        const res = NextResponse.next({ request: { headers: requestHeaders } });
        res.cookies.set('synapse_auth_cache', 'no_auth_required', {
            httpOnly: true,
            sameSite: 'lax',
            maxAge: 60,
            path: '/',
        });
        return res;
    }

    // Login required and user is not authenticated → redirect to /login
    const loginUrl = new URL('/login', request.url);
    loginUrl.searchParams.set('redirect', pathname + request.nextUrl.search);
    return NextResponse.redirect(loginUrl);
}

export const config = {
    matcher: [
        '/((?!_next/static|_next/image|favicon.ico|.*\\.(?:svg|png|jpg|jpeg|gif|webp)$).*)',
    ],
};
