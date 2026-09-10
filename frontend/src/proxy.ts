/**
 * Next.js Proxy (formerly Middleware)
 * -----------------------------------
 * Runs on the Node.js runtime (Next 16: proxy.ts defaults to Node, unlike the
 * legacy Edge middleware). This is REQUIRED so that SYNAPSE_INTERNAL_TOKEN and
 * SYNAPSE_JWT_SECRET are read from the live process.env at request time. These
 * secrets are generated per-install at runtime and must NOT be inlined at build
 * time (see next.config.ts — they are deliberately omitted from the `env` block).
 *
 * 1. Injects X-Synapse-Internal header for backend-proxied routes.
 * 2. Enforces login gate: redirects unauthenticated users to /login
 *    when login_enabled is configured in Synapse settings.
 *
 * Auth flow (per request):
 *   a. Check `synapse_session` cookie → verify JWT locally (no network call).
 *      This is only an optimization: it needs SYNAPSE_JWT_SECRET in this process.
 *   b. Check `synapse_auth_cache` cookie (60s TTL) → skip backend call if login is disabled
 *   c. Fetch /api/auth/status from backend (server-side, internal token injected).
 *      The backend is the single owner of SYNAPSE_JWT_SECRET, so this call ALSO
 *      validates the session (via the forwarded X-Synapse-Session header) and
 *      returns `authenticated`. This is the authoritative gate — it works even
 *      when the local fast-path can't (empty/mismatched secret in this process).
 *   d. If login required and not authenticated → redirect to /login?redirect=<original_path>
 *   e. If login not required → cache result for 60s and proceed
 */
import { NextRequest, NextResponse } from 'next/server';
import { jwtVerify } from 'jose';

const AUTH_BYPASS_PREFIXES = [
    '/login',
    '/api/auth/',
    '/api/v1/',   // external versioned API — Bearer API-key auth, not the login gate
    '/api/v2/',   // (kept in sync with InternalTokenMiddleware's /api/v\d+ skip)
    // The embeddable widget. Both of these are reached by a visitor on somebody
    // else's website who has no account here and never will, so the login gate
    // is not a check they could ever pass — it would redirect a customer's
    // paying visitors to our sign-in form.
    //
    // Bypassing it is not bypassing authentication, for the same reason
    // '/api/v1/' above is not: these routes carry their own credential. A widget
    // conversation is admitted by the parent origin matching the widget's
    // allow-list, and every call after that by a signed visitor token the
    // backend mints and verifies. See core/routes/widget.py.
    '/embed/',
    '/api/widget/',
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

/**
 * `frame-ancestors` for a widget's embed page.
 *
 * The browser-enforced half of the origin check: a page carrying this simply
 * will not paint inside a site that is not listed, whatever the embedding page
 * tries. It has to be a real response header — the spec ignores
 * `frame-ancestors` in a `<meta http-equiv>` tag — and a Server Component cannot
 * set one, so it is fetched here, before the page renders.
 *
 * Fails **closed**. The alternative to "nobody may embed this" is "anybody may",
 * which is not a thing to fall back to when the backend is slow.
 */
async function frameAncestors(publicKey: string, internalToken: string): Promise<string> {
    const backendUrl = process.env.BACKEND_URL || 'http://127.0.0.1:8765';
    try {
        const res = await fetch(
            `${backendUrl}/api/widget/${encodeURIComponent(publicKey)}/csp`,
            {
                headers: internalToken ? { 'X-Synapse-Internal': internalToken } : {},
                signal: AbortSignal.timeout(2000),
            },
        );
        if (res.ok) {
            const body = await res.json();
            if (typeof body.frame_ancestors === 'string') return body.frame_ancestors;
        }
    } catch {
        // fall through to 'none'
    }
    return "frame-ancestors 'none'";
}

export async function proxy(request: NextRequest) {
    const { pathname } = request.nextUrl;
    const internalToken = process.env.SYNAPSE_INTERNAL_TOKEN || '';

    // Always inject the internal token
    const requestHeaders = new Headers(request.headers);
    if (internalToken) {
        requestHeaders.set('X-Synapse-Internal', internalToken);
    }

    // The embed page is the one route that answers with a CSP of its own,
    // computed per widget. Handled before the bypass returns, so the header is
    // set on the response the visitor actually receives.
    const embedded = pathname.match(/^\/embed\/([^/]+)\/?$/);
    if (embedded) {
        const directive = await frameAncestors(decodeURIComponent(embedded[1]), internalToken);
        const res = NextResponse.next({ request: { headers: requestHeaders } });
        res.headers.set('Content-Security-Policy', directive);
        // Belt to the CSP's braces for anything old enough to honour one and not
        // the other. It can only say "nobody", so it is set only when that is
        // the answer.
        if (directive === "frame-ancestors 'none'") {
            res.headers.set('X-Frame-Options', 'DENY');
        }
        return res;
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

    // Cache miss: ask the backend whether login is required AND validate our
    // session. Forwarding the cookie lets the backend (the sole secret owner)
    // authenticate us even when the local fast-path above couldn't.
    const backendUrl = process.env.BACKEND_URL || 'http://127.0.0.1:8765';
    let loginRequired = false;
    let authenticated = false;
    try {
        const statusRes = await fetch(`${backendUrl}/api/auth/status`, {
            headers: {
                'X-Synapse-Internal': internalToken,
                ...(sessionCookie ? { 'X-Synapse-Session': sessionCookie } : {}),
                'Content-Type': 'application/json',
            },
            signal: AbortSignal.timeout(2000),
        });
        if (statusRes.ok) {
            const status = await statusRes.json();
            loginRequired = status.login_enabled === true && status.login_configured === true;
            authenticated = status.authenticated === true;
        }
    } catch {
        // Backend unreachable — fail open so we don't lock users out
        loginRequired = false;
    }

    // Not required, OR required but the backend validated our session → proceed.
    if (!loginRequired || authenticated) {
        const res = NextResponse.next({ request: { headers: requestHeaders } });
        if (!loginRequired) {
            // Cache only the "login disabled" result for 60s to avoid hitting the
            // backend on every request. Do NOT cache `authenticated` — JWTs are
            // stateless, so caching would delay logout taking effect.
            res.cookies.set('synapse_auth_cache', 'no_auth_required', {
                httpOnly: true,
                sameSite: 'lax',
                maxAge: 60,
                path: '/',
            });
        }
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
