/**
 * The backend's /auth routes (the Google sign-in and its callback), at the
 * address this server was started with (F309, `lib/backendProxy.ts`).
 */
import { proxyToBackend } from '@/lib/backendProxy';

export const dynamic = 'force-dynamic';

export {
    proxyToBackend as GET,
    proxyToBackend as HEAD,
    proxyToBackend as POST,
    proxyToBackend as PUT,
    proxyToBackend as PATCH,
    proxyToBackend as DELETE,
    proxyToBackend as OPTIONS,
};
