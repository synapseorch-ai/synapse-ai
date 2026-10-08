'use client';
/**
 * A connection on the open canvas that a run's signal can travel (D211, M3a-2).
 *
 * The same line as xyflow's smoothstep — its path, label, marker and hit area —
 * plus the live run: while a run is live, a connection the run came along is lit
 * in the machine's colour, and when a step starts, a dot travels the connection
 * that led to it, from the step that finished (Prototype 4's `travel`).
 *
 * The dot is moved along a hidden copy of the path frame by frame with
 * `getPointAtLength`, as the prototype does: no CSS file to sync, nothing to
 * time against an SVG document clock. `data.travel` is the run's, not the
 * edge's: it turns on once the run is live and past any replay, and the dot is
 * mounted from then on, holding the signal it found as history. A signal that
 * changes after that plays, so a reopened run lights in place and a live one
 * travels. Starts that land together leave about 120 ms apart, in the order they
 * came, so a chain of instant steps reads as a chain. Reduced motion never sets
 * `travel`.
 */
import { memo, useEffect, useRef } from 'react';
import { BaseEdge, getSmoothStepPath, type EdgeProps } from '@xyflow/react';

/** How long a signal takes to cross a connection, and how far apart two leave. */
const TRAVEL_MS = 650;
const STAGGER_MS = 120;

/** The next free moment for a signal to leave, shared by every connection on the page. */
let nextSlot = 0;

const ease = (p: number) => (p < 0.5 ? 2 * p * p : 1 - Math.pow(-2 * p + 2, 2) / 2);

export type SignalEdgeData = {
    /** Lit: the run came along this connection, and the run is live. */
    lit?: boolean;
    /** Which start sent the latest signal (its place in the run's trail); a new value plays it. */
    signal?: number | null;
    /** Whether signals travel now: the run is live, past any replay, and motion is allowed. */
    travel?: boolean;
    /** Which run the signals belong to. */
    runKey?: string | number;
};

function SignalDot({ path, signal }: { path: string; signal: number | null | undefined }) {
    const pathRef = useRef<SVGPathElement>(null);
    const dotRef = useRef<SVGCircleElement>(null);
    // The signal already on the edge when it mounted is history: it is lit, not played.
    const seen = useRef(signal);

    useEffect(() => {
        if (signal === seen.current || signal === null || signal === undefined) return;
        seen.current = signal;
        const line = pathRef.current;
        const dot = dotRef.current;
        if (!line || !dot || document.hidden) return;
        const now = performance.now();
        const start = Math.max(now, nextSlot);
        nextSlot = start + STAGGER_MS;
        let frame = 0;
        const length = line.getTotalLength();
        const step = (t: number) => {
            if (t < start) {
                frame = requestAnimationFrame(step);
                return;
            }
            const p = Math.min(1, (t - start) / TRAVEL_MS);
            const at = line.getPointAtLength(length * ease(p));
            dot.setAttribute('cx', String(at.x));
            dot.setAttribute('cy', String(at.y));
            dot.setAttribute('visibility', 'visible');
            if (p < 1) frame = requestAnimationFrame(step);
            else dot.setAttribute('visibility', 'hidden');
        };
        frame = requestAnimationFrame(step);
        return () => {
            cancelAnimationFrame(frame);
            dot.setAttribute('visibility', 'hidden');
        };
    }, [signal]);

    return (
        <g aria-hidden className="pointer-events-none">
            <path ref={pathRef} d={path} fill="none" stroke="none" />
            <circle
                ref={dotRef}
                r={4.5}
                visibility="hidden"
                data-signal-dot=""
                style={{
                    fill: 'var(--machine)',
                    stroke: 'var(--surface)',
                    strokeWidth: 2,
                    filter: 'drop-shadow(0 0 4px var(--machine))',
                }}
            />
        </g>
    );
}

function SignalEdgeComponent({
    sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition,
    markerEnd, markerStart, style, label, labelStyle, labelShowBg, labelBgStyle, labelBgPadding, labelBgBorderRadius,
    interactionWidth, data,
}: EdgeProps) {
    const [path, labelX, labelY] = getSmoothStepPath({ sourceX, sourceY, sourcePosition, targetX, targetY, targetPosition });
    const live = (data ?? {}) as SignalEdgeData;
    return (
        <>
            <BaseEdge
                path={path}
                markerEnd={markerEnd}
                markerStart={markerStart}
                style={{ ...style, ...(live.lit ? { stroke: 'var(--machine)' } : null), transition: 'stroke 300ms' }}
                label={label}
                labelX={labelX}
                labelY={labelY}
                labelStyle={labelStyle}
                labelShowBg={labelShowBg}
                labelBgStyle={labelBgStyle}
                labelBgPadding={labelBgPadding}
                labelBgBorderRadius={labelBgBorderRadius}
                interactionWidth={interactionWidth}
            />
            {/* A new run mounts a fresh dot, so its signals are never taken for the last run's. */}
            {live.travel && <SignalDot key={String(live.runKey ?? '')} path={path} signal={live.signal} />}
        </>
    );
}

export const SignalEdge = memo(SignalEdgeComponent);
