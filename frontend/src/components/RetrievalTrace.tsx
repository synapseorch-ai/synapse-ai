'use client';

/**
 * A search's retrieval trace (`core.knowledge.trace.Trace`) as a list of facts:
 * each leg and what it returned, the fusion, the rerank, every cut and why, the
 * index version each knowledge base answered from, the time, and what it cost.
 * A Retrieve step's run log also wants what degraded and the passages it
 * answered with (`withDegraded`, `withHits`); the test search shows both
 * itself, above the trace.
 *
 * Plain and presentational — it draws a trace the caller already holds (the
 * test search's) or fetched (`getQueryTrace`, the run log's) — so it syncs to
 * the cloud unchanged.
 */

import { DEGRADED, type SearchTrace } from '@/lib/knowledge';
import { cn } from '@/lib/cn';

const TIMED = ['embed', 'lexical', 'vector', 'fuse', 'rerank'] as const;

function usd(n: number): string {
    return n > 0 && n < 0.0001 ? `$${n.toFixed(6)}` : `$${n.toFixed(4)}`;
}

/** A score as read: a keyword score can be ~1e-6 on a small base, which three decimals would show as 0. */
function score(n: number): string {
    return n !== 0 && Math.abs(n) < 0.001 ? n.toExponential(2) : n.toFixed(3);
}

function pages(start?: number | null, end?: number | null): string {
    if (!start) return '';
    return end && end !== start ? ` pp. ${start}–${end}` : ` p. ${start}`;
}

function Row({ label, dense, children }: { label: string; dense?: boolean; children: React.ReactNode }) {
    return (
        <div className={cn('flex justify-between gap-4', dense ? 'py-0.5' : 'py-1 text-sm')}>
            <dt className="shrink-0 text-text-muted">{label}</dt>
            <dd className="min-w-0 break-words text-right text-text">{children}</dd>
        </div>
    );
}

export function RetrievalTrace({ trace, withDegraded = false, withHits = false, dense = false, className }: {
    trace: SearchTrace;
    /** Each degradation in words (`DEGRADED`). */
    withDegraded?: boolean;
    /** The `[K#]` passages it answered with: label, place and score. */
    withHits?: boolean;
    /** Tighter rows at the surrounding size, for a run log. */
    dense?: boolean;
    className?: string;
}) {
    const timings = TIMED.filter((k) => (trace.timings_ms?.[k] ?? 0) > 0)
        .map((k) => `${k} ${trace.timings_ms[k]}`).join(' · ');
    const versions = Object.entries(trace.pipeline || {});
    const costs = trace.costs;
    return (
        <dl className={cn('space-y-0.5', className)}>
            {withDegraded && trace.degraded.map((d) => (
                <Row key={`degraded-${d.code}`} label="Degraded" dense={dense}>{DEGRADED[d.code] ?? d.code}</Row>
            ))}
            {trace.legs.map((leg, i) => (
                <Row key={`leg-${i}`} label={`Leg: ${leg.leg}`} dense={dense}>
                    {leg.returned} of {leg.requested}{leg.filtered ? ', filtered in the index' : ''}
                    {` · ${leg.latency_ms} ms`}
                </Row>
            ))}
            {trace.fusion && (
                <Row label="Fusion" dense={dense}>
                    {`reciprocal rank, k = ${trace.fusion.params.k}`}
                    {Object.keys(trace.fusion.params.weights).length > 0 && ` · ${Object.entries(trace.fusion.params.weights)
                        .map(([leg, w]) => `${leg} ×${w}`).join(', ')}`}
                </Row>
            )}
            {trace.rerank && (
                <Row label={`Rerank: ${trace.rerank.model}`} dense={dense}>
                    {trace.rerank.results.length} of {trace.rerank.input_count} · {trace.rerank.latency_ms} ms
                </Row>
            )}
            {trace.cuts.map((cut, i) => (
                <Row key={`cut-${i}`} label={`Cut: ${cut.stage}`} dense={dense}>
                    {cut.dropped_count} dropped{cut.rule ? ` (${cut.rule})` : ''}
                </Row>
            ))}
            {trace.legs.length === 0 && trace.cuts.length === 0 && (
                <Row label="Legs" dense={dense}>None ran</Row>
            )}
            {versions.map(([kbId, p]) => (
                <Row key={`version-${kbId}`} label="Index version" dense={dense}>
                    <span className="font-mono" translate="no" title={kbId}>{p.snapshot_id}</span>
                </Row>
            ))}
            {timings && <Row label="Time (ms)" dense={dense}>{timings}</Row>}
            {costs && (
                <Row label="Cost" dense={dense}>
                    {usd(costs.total_est_usd)}
                    {costs.total_charged_microusd != null && ` (charged ${usd(costs.total_charged_microusd / 1_000_000)})`}
                    {costs.rows.length > 0 && ` · ${costs.rows.map((r) => `${r.stage} ${r.model} (${r.cost_source})`).join(', ')}`}
                </Row>
            )}
            {withHits && (trace.hits || []).map((hit) => (
                <Row key={`hit-${hit.label}`} label={hit.label} dense={dense}>
                    <span className="font-mono" translate="no">{hit.path}{pages(hit.page_start, hit.page_end)}</span>
                    {` · ${score(hit.score)}`}
                </Row>
            ))}
        </dl>
    );
}
