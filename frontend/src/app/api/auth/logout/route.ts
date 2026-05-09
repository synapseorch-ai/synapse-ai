/**
 * POST /api/auth/logout
 * Clears session and auth-cache cookies. Stateless — no backend call needed.
 */
import { NextResponse } from 'next/server';

export const dynamic = 'force-dynamic';

export async function POST() {
    const response = NextResponse.json({ success: true });
    response.cookies.set('synapse_session', '', { httpOnly: true, sameSite: 'lax', maxAge: 0, path: '/' });
    response.cookies.set('synapse_auth_cache', '', { maxAge: 0, path: '/' });
    return response;
}
