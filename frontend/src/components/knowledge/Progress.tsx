'use client';

/*
 * How far Knowledge's work has got (PG1, F357): a bar for an upload, a working document and a rebuild.
 *
 * Work that can be counted fills the bar; work that cannot (a reader that takes the file in one pass, the
 * index's one write) pulses instead — still, for a person who asked for less motion. Shared with the cloud app
 * byte for byte.
 */
import { cn } from '@/lib/cn';

export function ProgressBar({ value, max, label, className }: {
    value?: number | null;
    max?: number | null;
    /** What the bar measures, for a screen reader: "Uploading report.pdf". */
    label: string;
    className?: string;
}) {
    const counted = value != null && max != null && max > 0;
    const share = counted ? Math.min(100, Math.max(0, (value / max) * 100)) : 0;
    return (
        <div
            role="progressbar"
            aria-label={label}
            aria-valuemin={counted ? 0 : undefined}
            aria-valuemax={counted ? max : undefined}
            aria-valuenow={counted ? Math.min(value, max) : undefined}
            className={cn('h-1.5 overflow-hidden rounded-full bg-surface-2', className)}
        >
            {counted ? (
                <div className="h-full bg-accent transition-[width] motion-reduce:transition-none" style={{ width: `${share}%` }} />
            ) : (
                <div className="h-full w-full animate-pulse bg-accent/60 motion-reduce:animate-none" />
            )}
        </div>
    );
}
