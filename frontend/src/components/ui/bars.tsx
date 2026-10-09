/**
 * Quantities as bars: a `Meter` (how much of something is used) and a
 * `BarList` (several things compared, largest first).
 *
 * UX1 found Usage drawing each model's bar in its provider's hue — seven
 * colours for one comparison, none of them meaning anything. A comparison is
 * one hue (`--machine`, the product at work) with the label and the figure
 * written beside each bar, so nothing has to be read off an axis or a legend
 * (M2g). The figures stay in the text colour.
 *
 * No "use client": no hooks, so a server page can draw them.
 */
import * as React from "react";

import { cn } from "@/lib/cn";

/** A share of a whole, clamped to 0–100 and never a sliver short of empty. */
function pct(value: number, of: number): number {
  if (!(of > 0) || !(value > 0)) return 0;
  return Math.min(100, Math.max(0.75, (value / of) * 100));
}

/**
 * One quantity against its limit: "Execution credits — 65 used of 150,000".
 * `value` is the words on the right; the bar is `used / of`.
 */
export function Meter({
  label,
  used,
  of,
  value,
  sub,
  className,
}: {
  label: string;
  used: number;
  of: number;
  /** The figure as read ("65 used of 150,000"). */
  value: React.ReactNode;
  /** One quiet line under the bar. */
  sub?: React.ReactNode;
  className?: string;
}) {
  const p = pct(used, of);
  return (
    <div className={cn("grid min-w-0 gap-1.5", className)}>
      <div className="flex min-w-0 items-baseline justify-between gap-3 text-[13px]">
        <span className="truncate text-text-muted">{label}</span>
        <span className="shrink-0 tabular-nums text-text">{value}</span>
      </div>
      <div
        role="meter"
        aria-label={label}
        aria-valuemin={0}
        aria-valuemax={of > 0 ? of : 0}
        aria-valuenow={Math.min(used, of > 0 ? of : used)}
        className="h-1.5 overflow-hidden rounded-full bg-surface-2"
      >
        <div className="h-full rounded-full bg-machine" style={{ width: `${p}%` }} />
      </div>
      {sub && <div className="min-w-0 text-xs text-text-faint">{sub}</div>}
    </div>
  );
}

export type BarRow = {
  key: string;
  label: React.ReactNode;
  /** A second, quieter line under the label ("140 calls · 564,915 tokens"). */
  sub?: React.ReactNode;
  /** What the bar measures. */
  value: number;
  /** The figure written at the end of the row. */
  display: React.ReactNode;
};

/**
 * Several things compared on one hue, each bar's length against the largest.
 * A row is label | bar | figure from `sm`; on a phone the bar drops under the
 * label so the names are not squeezed.
 */
export function BarList({ rows, label, className }: { rows: BarRow[]; label: string; className?: string }) {
  const max = rows.reduce((m, r) => Math.max(m, r.value), 0);
  return (
    <ul aria-label={label} className={cn("m-0 grid list-none gap-0 p-0", className)}>
      {rows.map(r => (
        <li
          key={r.key}
          data-bar-row
          className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-x-4 gap-y-1.5 py-2.5 sm:grid-cols-[minmax(12rem,22rem)_minmax(0,1fr)_9rem]"
        >
          <span className="min-w-0">
            <span className="block truncate text-sm font-medium text-text">{r.label}</span>
            {r.sub && <span className="block truncate text-xs text-text-faint">{r.sub}</span>}
          </span>
          <span aria-hidden className="col-span-2 row-start-2 h-2 overflow-hidden rounded-full bg-surface-2 sm:col-span-1 sm:row-start-auto">
            <span className="block h-full rounded-full bg-machine" style={{ width: `${pct(r.value, max)}%` }} />
          </span>
          <span className="col-start-2 row-start-1 min-w-[5.5rem] whitespace-nowrap text-right text-sm tabular-nums text-text sm:col-start-auto sm:row-start-auto">
            {r.display}
          </span>
        </li>
      ))}
    </ul>
  );
}
