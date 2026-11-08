/**
 * A row of headline numbers as one strip, not a row of tiles (UX1, M2a).
 *
 * UX1 found Home's four bordered tiles labelled in four hues at equal weight —
 * status colours outshouting the brand, and boxes inside boxes. A strip is one
 * object: a rule above and below, hairlines between the cells, every number in
 * the text colour. The colour that remains is a dot beside the label, and only
 * where the number means something (cyan the machine at work, amber a person,
 * red a failure, green an outcome). Four across; two by two on a phone.
 *
 * No "use client": it renders no hooks, so a server page can draw it.
 */
import * as React from "react";

import { cn } from "@/lib/cn";

export type StatDot = "machine" | "person" | "danger" | "success" | "neutral";

export type StatCell = {
  label: string;
  value: React.ReactNode;
  /** One quiet line under the number: what it is out of, when, or a link. */
  sub?: React.ReactNode;
  /** The meaning, as a dot beside the label. No dot: a plain figure. */
  dot?: StatDot;
};

const DOT: Record<StatDot, string> = {
  machine: "bg-machine",
  person: "bg-person",
  danger: "bg-danger",
  success: "bg-success",
  neutral: "bg-text-faint",
};

export function StatStrip({
  cells,
  size = "md",
  label,
  className,
}: {
  cells: StatCell[];
  /** `md` is a page's hero figures (30px); `sm` a secondary strip (20px). */
  size?: "sm" | "md";
  /** What the strip is, for a screen reader ("This week"). */
  label?: string;
  className?: string;
}) {
  return (
    <dl
      aria-label={label}
      style={{ "--stat-cols": cells.length } as React.CSSProperties}
      className={cn(
        "m-0 grid grid-cols-2 border-y border-border sm:[grid-template-columns:repeat(var(--stat-cols),minmax(0,1fr))]",
        className,
      )}
    >
      {cells.map((cell, i) => (
        <div
          key={cell.label}
          className={cn(
            "grid min-w-0 content-start gap-0.5 border-border pr-[18px]",
            size === "md" ? "py-4" : "py-3",
            // Two by two on a phone: the second of each pair has the hairline, the second row a rule above.
            i % 2 === 1 && "border-l pl-[18px]",
            i >= 2 && "border-t",
            // One row from `sm`: every cell after the first has the hairline, and none has a rule above.
            i > 0 && "sm:border-l sm:pl-[18px]",
            "sm:border-t-0",
          )}
        >
          <dt className="flex min-w-0 items-center gap-[7px] text-[12.5px] text-text-muted">
            {cell.dot && <span aria-hidden className={cn("size-2 shrink-0 rounded-full", DOT[cell.dot])} />}
            <span className="truncate">{cell.label}</span>
          </dt>
          <dd
            className={cn(
              "m-0 font-semibold tabular-nums text-text",
              size === "md" ? "text-[30px] leading-[1.15] tracking-[-0.035em]" : "text-xl leading-snug tracking-[-0.02em]",
            )}
          >
            {cell.value}
          </dd>
          {cell.sub && <dd className="m-0 min-w-0 truncate text-xs text-text-faint">{cell.sub}</dd>}
        </div>
      ))}
    </dl>
  );
}
