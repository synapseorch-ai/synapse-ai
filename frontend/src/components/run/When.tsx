'use client';

/*
 * Dates and durations that do not break hydration (M2b).
 *
 * Cloud renders its run pages on the server, whose time zone is UTC and whose
 * "now" is not the browser's, so "Today 7:25 PM" printed there would not match
 * the browser's first render and React would throw the tree away (cloud's
 * `LocalTime` and F147's `LiveDuration` say the same). So until the page has
 * mounted, `useClock` reports no "now" and the words are absolute and in UTC;
 * once mounted they are the reader's own, and the clock ticks.
 */
import { useEffect, useState } from 'react';

import { duration, elapsed, fullWhen, timeOfDay, UNMOUNTED_CLOCK, when, type Clock } from './model';

/** The reader's clock once mounted, re-read every `everyMs`. */
export function useClock(everyMs = 30_000): Clock {
    const [clock, setClock] = useState<Clock>(UNMOUNTED_CLOCK);
    useEffect(() => {
        const tick = () => setClock({ now: Date.now() });
        tick();
        const timer = window.setInterval(tick, everyMs);
        return () => window.clearInterval(timer);
    }, [everyMs]);
    return clock;
}

/** "Today 7:25 PM", the full instant on hover (UX1's dates). */
export function When({ iso, clock, className }: { iso: string | null | undefined; clock: Clock; className?: string }) {
    if (!iso) return <span className={className}>—</span>;
    return (
        <time dateTime={iso} title={fullWhen(iso, clock)} className={className} suppressHydrationWarning>
            {when(iso, clock)}
        </time>
    );
}

/** "7:25 PM", for a row under its day's heading; the full instant on hover. */
export function TimeOfDay({ iso, clock, className }: { iso: string | null | undefined; clock: Clock; className?: string }) {
    if (!iso) return <span className={className}>—</span>;
    return (
        <time dateTime={iso} title={fullWhen(iso, clock)} className={className} suppressHydrationWarning>
            {timeOfDay(iso, clock)}
        </time>
    );
}

/**
 * How long a run took, or has taken so far. A finished run prints its fixed
 * duration everywhere; an unfinished one prints "—" until mounted, then counts.
 */
export function Took({ start, end }: { start: string | null | undefined; end: string | null | undefined }) {
    const fixed = Boolean(end) || !start;
    const [now, setNow] = useState<number | null>(null);
    useEffect(() => {
        if (fixed) return;
        const tick = () => setNow(Date.now());
        tick();
        const timer = window.setInterval(tick, 1000);
        return () => window.clearInterval(timer);
    }, [fixed]);
    if (fixed) return <>{duration(elapsed(start, end, 0))}</>;
    return <span suppressHydrationWarning>{now === null ? '—' : duration(elapsed(start, null, now))}</span>;
}
