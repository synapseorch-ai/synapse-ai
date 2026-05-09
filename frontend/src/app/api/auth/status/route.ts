/**
 * GET /api/auth/status
 * Proxies to backend — returns {login_enabled, login_configured}.
 */
import { NextResponse } from 'next/server';
import { BACKEND_URL, backendHeaders } from '@/lib/backend';

export const dynamic = 'force-dynamic';

export async function GET() {
    try {
        const res = await fetch(`${BACKEND_URL}/api/auth/status`, {
            headers: backendHeaders(),
        });
        if (!res.ok) {
            return NextResponse.json({ login_enabled: false, login_configured: false });
        }
        return NextResponse.json(await res.json());
    } catch {
        return NextResponse.json({ login_enabled: false, login_configured: false });
    }
}
