/**
 * The login gate as the browser sees it.
 *
 * `/api/auth/status` is outside the gate (proxy.ts), so it answers whether or
 * not this browser is signed in. The rail reads it to decide whether there is
 * anything to sign out of (F221), and reads it again whenever Settings changes
 * the login, which says so with `AUTH_CHANGED_EVENT`.
 */
export type AuthStatus = {
    login_enabled: boolean;
    login_configured: boolean;
    access_required: boolean;
    authenticated: boolean;
};

export const AUTH_CHANGED_EVENT = 'synapse-auth-changed';

export async function readAuthStatus(): Promise<AuthStatus | null> {
    try {
        const res = await fetch('/api/auth/status', { cache: 'no-store' });
        return res.ok ? ((await res.json()) as AuthStatus) : null;
    } catch {
        return null;
    }
}

/** A password gate, and this browser is through it. */
export function canSignOut(status: AuthStatus | null): boolean {
    return !!status && status.login_enabled && status.login_configured && status.authenticated;
}
