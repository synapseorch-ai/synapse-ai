/**
 * The estimate route (CE1, D261): what a run will cost in models, from the rate
 * card. The editor's Run now and settings, and the builder's plan card, read it
 * through `components/orchestration/estimate`.
 */
import type { EstimateLoader } from '@/components/orchestration/estimate';

export const loadRunEstimate: EstimateLoader = async (body, signal) => {
    const res = await fetch('/api/orchestrations/estimate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
        signal,
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return res.json();
};
