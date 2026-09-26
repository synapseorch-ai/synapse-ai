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
 *   b. Check this process's own 60s cache of "login is disabled" → skip the backend
 *      call. Never a cookie: the client could set one (S9, below).
 *   c. Fetch /api/auth/status from backend (server-side, internal token injected).
 *      The backend is the single owner of SYNAPSE_JWT_SECRET, so this call ALSO
 *      validates the session (via the forwarded X-Synapse-Session header) and
 *      returns `authenticated`. This is the authoritative gate — it works even
 *      when the local fast-path can't (empty/mismatched secret in this process).
 *   d. If login required and not authenticated → a page is redirected to
 *      /login?redirect=<original_path>; an /api/* call gets 401 JSON (F178)
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
    // NOTE: `/embed/preview/` is deliberately NOT bypassed — see
    // `shouldBypassAuth`. It is the owner's own preview and must stay behind the
    // login gate; bypassing it would make it a way to talk to any widget without
    // being on that widget's allowed list.
    '/auth/',
    '/_next/',
    '/favicon',
];

/**
 * When this process last learned that login is off, plus 60s. Process memory,
 * not a cookie: the client can set any cookie it likes (S9).
 */
const LOGIN_OFF_CACHE_MS = 60_000;
let loginOffUntil = 0;

/** Paths that look bypassed by prefix but must not be. */
const AUTH_BYPASS_EXCEPTIONS = ['/embed/preview/'];

/**
 * Platform webhooks (Telegram, Slack, Discord, Teams, WhatsApp): posted by the
 * platform, never a browser, and verified by each platform's own check. The
 * rest of `/api/messaging/` stays behind the gate. Mirrors the backend's
 * `_WEBHOOK` in core/internal_auth.py.
 */
const WEBHOOK = /^\/api\/messaging\/[^/]+\/webhook(\/|$)/;

function shouldBypassAuth(pathname: string): boolean {
    if (AUTH_BYPASS_EXCEPTIONS.some(p => pathname.startsWith(p))) return false;
    if (WEBHOOK.test(pathname)) return true;
    return AUTH_BYPASS_PREFIXES.some(p => pathname.startsWith(p));
}

async function verifyJwt(token: string, secret: string): Promise<boolean> {
    try {
        const { payload } = await jwtVerify(token, new TextEncoder().encode(secret), {
            algorithms: ['HS256'],
            issuer: 'synapse',
        });
        // An access-link session (D124) is valid only while its token is still
        // the install's and login is still off — facts only the backend holds.
        // Not trusted here: it falls through to /api/auth/status, so a rotated
        // link or a newly-set password sends the browser to the gate rather than
        // to pages whose every call the backend refuses.
        return payload.atf === undefined;
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
    // And the session, which the backend checks for itself when login is on
    // (S9): the internal token proves a request came through here, not that a
    // signed-in person sent it. Only ever the browser's own cookie — a
    // client-supplied header of the same name is replaced, never passed on.
    requestHeaders.delete('X-Synapse-Session');
    const forwardedSession = request.cookies.get('synapse_session')?.value;
    if (forwardedSession) {
        requestHeaders.set('X-Synapse-Session', forwardedSession);
    }

    // The owner's own preview. Framed by our settings screen and nobody else's
    // page — and it stays behind the login gate, because `shouldBypassAuth`
    // excludes this prefix. Placed before the visitor branch below so the
    // narrower path wins.
    if (pathname.startsWith('/embed/preview/')) {
        const res = NextResponse.next({ request: { headers: requestHeaders } });
        res.headers.set('Content-Security-Policy', "frame-ancestors 'self'");
        return res;
    }

    // The embed page is the one route that answers with a CSP of its own,
    // computed per widget. Handled before the bypass returns, so the header is
    // set on the response the visitor actually receives.
    // `v1.js` is the loader, not an embed page: it is a route handler, and it
    // is fetched by every visitor of every customer site. Excluding it here
    // saves each of them a round trip for a header that means nothing on a
    // script.
    const embedded =
        pathname === "/embed/v1.js" ? null : pathname.match(/^\/embed\/([^/]+)\/?$/);
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

    // The access link (login off, D124): a page opened with `?token=` swaps it
    // for a session at /api/auth/access, which comes straight back here with
    // the token gone from the URL — so it never sits in history or a log line.
    const linkToken = request.nextUrl.searchParams.get('token');
    if (linkToken && !pathname.startsWith('/api/')) {
        const clean = new URL(request.nextUrl);
        clean.searchParams.delete('token');
        const exchange = new URL('/api/auth/access', request.url);
        exchange.searchParams.set('token', linkToken);
        exchange.searchParams.set('redirect', clean.pathname + clean.search);
        return NextResponse.redirect(exchange);
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

    // Cache hit: this process asked the backend under a minute ago and login
    // was off. S9: this used to be a `synapse_auth_cache=no_auth_required`
    // cookie, and a client that set it itself skipped the gate on a
    // login-protected install. Nothing the client sends reaches this cache.
    if (Date.now() < loginOffUntil) {
        return NextResponse.next({ request: { headers: requestHeaders } });
    }

    // Cache miss: ask the backend whether login is required AND validate our
    // session. Forwarding the cookie lets the backend (the sole secret owner)
    // authenticate us even when the local fast-path above couldn't.
    const backendUrl = process.env.BACKEND_URL || 'http://127.0.0.1:8765';
    let loginRequired = false;
    let authenticated = false;
    let answered = false;
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
            // A password, or — with login off — this install's access link.
            loginRequired =
                (status.login_enabled === true && status.login_configured === true) ||
                status.access_required === true;
            authenticated = status.authenticated === true;
            answered = true;
        }
    } catch {
        // Backend unreachable — fail open so we don't lock users out
        loginRequired = false;
    }

    // Not required, OR required but the backend validated our session → proceed.
    if (!loginRequired || authenticated) {
        if (!loginRequired && answered) {
            // Cache only the backend's own answer — never the fail-open guess.
            // Cache only the "login disabled" result, for 60s, to avoid hitting
            // the backend on every request. Do NOT cache `authenticated` — JWTs
            // are stateless, so caching would delay logout taking effect. A
            // login switched on inside the window is still enforced: the
            // backend checks the session itself.
            loginOffUntil = Date.now() + LOGIN_OFF_CACHE_MS;
        }
        const res = NextResponse.next({ request: { headers: requestHeaders } });
        // Clear the cookie older builds set, so nothing still carries it.
        if (request.cookies.has('synapse_auth_cache')) {
            res.cookies.delete('synapse_auth_cache');
        }
        return res;
    }

    // Login required and user is not authenticated. An API call is answered
    // as one, with the backend's own 401 for the same condition (F178): the
    // redirect sent fetches and streams a 10 KB sign-in page they could not
    // read, and a stream retrying that every 5 s cost a page render and a
    // backend call each time for as long as the tab stayed open.
    if (pathname.startsWith('/api/')) {
        return NextResponse.json({ detail: 'Login required' }, { status: 401 });
    }
    // A page → the sign-in form, and back here after it.
    const loginUrl = new URL('/login', request.url);
    loginUrl.searchParams.set('redirect', pathname + request.nextUrl.search);
    return NextResponse.redirect(loginUrl);
}

export const config = {
    matcher: [
        '/((?!_next/static|_next/image|favicon.ico|.*\\.(?:svg|png|jpg|jpeg|gif|webp)$).*)',
    ],
};
