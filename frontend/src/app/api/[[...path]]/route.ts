/**
 * Every /api request no route handler of ours answers goes on to the backend,
 * at the address this server was started with (F309, `lib/backendProxy.ts`).
 *
 * A more specific route handler always wins over this catch-all — the order
 * the fallback rewrite it replaces had. Every method is forwarded, HEAD and
 * OPTIONS included, so none is answered here instead of by the backend.
 */
import { proxyToBackend } from '@/lib/backendProxy';

export const dynamic = 'force-dynamic';
export const maxDuration = 610;

export {
    proxyToBackend as GET,
    proxyToBackend as HEAD,
    proxyToBackend as POST,
    proxyToBackend as PUT,
    proxyToBackend as PATCH,
    proxyToBackend as DELETE,
    proxyToBackend as OPTIONS,
};
