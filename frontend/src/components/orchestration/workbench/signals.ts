'use client';
/**
 * The live run on the open canvas's connections (D211, M3a-2): which connection
 * each step's start came along, lit while the run is live, with a signal that
 * travels it (`SignalEdge`).
 *
 * The product passes the run's trail — the step ids in the order they started —
 * and this works out the rest, purely: for every start, the nearest earlier step
 * in the trail with a connection into it is the one it came from. That covers a
 * chain, a Parallel's branches (from the Parallel) and its join (from the
 * Parallel again, once its branches are done), a Loop's body each time round and
 * its way on, a route an evaluator, If/Else or Switch took, and a person's answer
 * (the step that asked, then the next). Nothing is diffed between renders: a
 * connection's `signal` is the index of the latest start that came along it,
 * and the edge plays a signal when that number changes.
 */
import { useMemo } from 'react';
import type { Edge, EdgeMarker, EdgeTypes } from '@xyflow/react';
import { SignalEdge, type SignalEdgeData } from '../SignalEdge';

/** Module-level, so xyflow sees the same object on every render. */
export const signalEdgeTypes: EdgeTypes = { signal: SignalEdge };

/** How far back in the trail a start looks for the step it came from. */
const LOOKBACK = 64;

/** Per connection, the trail index of the latest start that came along it. */
export function signalsOf(edges: readonly Pick<Edge, 'id' | 'source' | 'target'>[], trail: readonly string[]): Map<string, number> {
    const into = new Map<string, Map<string, string>>();
    for (const e of edges) {
        let from = into.get(e.target);
        if (!from) into.set(e.target, (from = new Map()));
        if (!from.has(e.source)) from.set(e.source, e.id);
    }
    const out = new Map<string, number>();
    trail.forEach((stepId, i) => {
        const from = into.get(stepId);
        if (!from) return;
        for (let j = i - 1; j >= 0 && j >= i - LOOKBACK; j--) {
            const edgeId = from.get(trail[j]);
            if (edgeId) {
                out.set(edgeId, i);
                return;
            }
        }
    });
    return out;
}

/**
 * The canvas's edges as signal edges: lit where the run came while it is live,
 * each with its latest signal, and the run's say on whether signals travel now.
 */
export function useRunEdges(edges: Edge[], run: {
    trail?: readonly string[];
    /** The run is going or waiting: what it came along is lit. */
    live: boolean;
    /** Signals travel: live, past any replay, motion allowed. */
    travel: boolean;
    /** A new run starts its signals afresh. */
    runKey?: string | number;
}): Edge[] {
    const { trail, live, travel, runKey } = run;
    const signals = useMemo(() => signalsOf(edges, trail ?? []), [edges, trail]);
    return useMemo(() => edges.map((e) => {
        const at = signals.get(e.id);
        const lit = live && at !== undefined;
        const data: SignalEdgeData = {
            ...(e.data ?? {}), lit, signal: at ?? null, travel, runKey,
        };
        const marker = e.markerEnd;
        const markerEnd = lit && marker && typeof marker === 'object'
            ? { ...(marker as EdgeMarker), color: 'var(--machine)' }
            : marker;
        return { ...e, type: 'signal', data, markerEnd };
    }), [edges, signals, live, travel, runKey]);
}
