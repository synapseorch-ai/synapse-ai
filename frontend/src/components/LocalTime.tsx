"use client";

/**
 * An absolute timestamp in the reader's own time, without breaking hydration.
 *
 * Several screens printed UTC with no zone next to the rest of the app's local
 * times: Evals ("2026-09-25 10:53") and Runs & Logs ("2026-09-25 10:08:03"), in
 * IST read as five and a half hours out (F205). Ported from cloud's
 * `components/LocalTime.tsx`.
 *
 * `new Date(iso).toLocaleString()` is evaluated twice, once on the server (UTC)
 * and once in the browser, so the two strings differ and React discards the
 * render. The server emits UTC, marked as such, and the local time replaces it
 * once mounted. `<time dateTime>` keeps the exact instant in the markup.
 */

import { useSyncExternalStore } from "react";

export type LocalTimeFormat = "datetime" | "date" | "medium" | "medium-time";

const LOCAL: Record<LocalTimeFormat, (d: Date) => string> = {
    datetime: (d) => d.toLocaleString(),
    date: (d) => d.toLocaleDateString(),
    medium: (d) => new Intl.DateTimeFormat(undefined, { dateStyle: "medium" }).format(d),
    "medium-time": (d) =>
        new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(d),
};

/**
 * An instant from a stamp the backend wrote. The run logs write UTC with no
 * zone (`2026-09-25 10:08:03`), which `new Date` reads as local time, so a
 * stamp with no zone is taken as UTC here. One with a zone is left alone.
 */
export function parseStamp(stamp: string): Date {
    const s = stamp.trim();
    const bare = /^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2}(\.\d+)?)?$/.test(s);
    return new Date(bare ? `${s.replace(" ", "T")}Z` : s);
}

const subscribeNever = () => () => {};

export function LocalTime({
    iso,
    format = "datetime",
    className,
}: {
    iso: string | null | undefined;
    format?: LocalTimeFormat;
    className?: string;
}) {
    // The server rendering, and the hydrating one, are the UTC text; React then
    // renders the client snapshot, the local time. Nothing to subscribe to.
    const d = iso ? parseStamp(iso) : null;
    const valid = !!d && !Number.isNaN(d.getTime());
    const text = useSyncExternalStore(
        subscribeNever,
        () => (iso ? (valid ? LOCAL[format](d!) : iso) : "—"),
        () => (iso ? utc(iso, format) : "—"),
    );

    if (!iso || !d) return <>—</>;
    return (
        <time dateTime={Number.isNaN(d.getTime()) ? iso : d.toISOString()} className={className} suppressHydrationWarning>
            {text}
        </time>
    );
}

/**
 * `2026-08-22 02:24 UTC`, or `2026-08-22` for a date — stable in every timezone,
 * including the server's.
 */
function utc(iso: string, format: LocalTimeFormat): string {
    const d = parseStamp(iso);
    if (Number.isNaN(d.getTime())) return iso;
    if (format === "date" || format === "medium") return d.toISOString().slice(0, 10);
    return `${d.toISOString().slice(0, 16).replace("T", " ")} UTC`;
}
