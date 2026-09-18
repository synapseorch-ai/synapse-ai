/**
 * The table.
 *
 * There were two of them: `DataTable`, and a `th`/`td` pair of class strings
 * that eight screens hand-assembled a `<table>` around. They disagreed on cell
 * padding (`px-5 py-2.5` against `px-4 py-3`), on whether rows highlight under
 * the cursor (only one did), and on whether the header was separated from the
 * body by anything but a hairline (neither). `DataTable` is now built from
 * these parts, so there is one answer.
 *
 * ## Density without context
 *
 * `density` sets two CSS custom properties on the `<table>` and the cells read
 * them. A React context would be the obvious way and cannot be used here:
 * `createContext` is not available in a server component, and half these tables
 * — the dashboard, the run detail, every staff screen — are server-rendered
 * with no client boundary at all. Custom properties inherit through the DOM,
 * which is the same mechanism with none of the cost.
 *
 * ## Why the action column stopped floating away
 *
 * A `<table>` distributes leftover width across every column, so on a wide
 * monitor a four-column table pushes "Respond" into the far corner while the
 * content columns stay narrow — the row reads as two things at opposite ends of
 * the screen with a void between. `width="full"` gives the slack to one primary
 * column and `width="min"` shrinks the rest to their content, so actions sit
 * beside what they act on no matter how wide the window gets.
 */

import * as React from "react";

import { cn } from "@/lib/cn";

export type Density = "comfortable" | "compact";

const DENSITY: Record<Density, React.CSSProperties> = {
  comfortable: {
    "--cell-px": "1.25rem",
    "--cell-py": "0.875rem",
    "--head-py": "0.625rem",
  } as React.CSSProperties,
  compact: {
    "--cell-px": "1rem",
    "--cell-py": "0.5rem",
    "--head-py": "0.5rem",
  } as React.CSSProperties,
};

const cellPadding = "px-[var(--cell-px)] py-[var(--cell-py)]";

/**
 * Column width.
 *
 *   full — takes all the leftover space. One per table: the name, the thing
 *          the row is about.
 *   min  — shrinks to its content and stays there. Status, timestamps, actions.
 *   auto — the browser decides. The default, and rarely what you want in a
 *          table wide enough to scroll.
 */
export type ColumnWidth = "auto" | "full" | "min";

const WIDTHS: Record<ColumnWidth, string> = {
  auto: "",
  full: "w-full",
  min: "w-px whitespace-nowrap",
};

/**
 * `min` only shrinks a column to its content if the content refuses to wrap.
 * Leave it out of the cells and the browser accepts the invitation to make the
 * column narrower still by breaking the text — which is how a two-word status
 * ends up on two lines and one row taller than its neighbours. Header and body
 * cells both need it, so `TD` should carry `nowrap` wherever `TH` carries
 * `width="min"`.
 */

export function Table({
  children,
  density = "comfortable",
  minWidth = "42rem",
  className,
}: {
  children: React.ReactNode;
  density?: Density;
  /** Below this the table scrolls inside its own box rather than squashing. */
  minWidth?: string;
  className?: string;
}) {
  return (
    // The scroll container, so wide content never makes the page body scroll.
    // `relative` because a scroll container must also contain what it scrolls:
    // an `sr-only` header cell is `position: absolute`, and with no positioned
    // ancestor it is placed against the page — cloud's staff tables slid
    // sideways 125px at phone width for an "Actions" header nobody can see (F34).
    <div className="scroll-x relative">
      <table
        style={{ ...DENSITY[density], minWidth }}
        className={cn("w-full border-collapse text-left", className)}
      >
        {children}
      </table>
    </div>
  );
}

export function THead({
  children,
  className,
}: {
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <thead className={cn("border-b border-border bg-surface-header", className)}>{children}</thead>
  );
}

export function TBody({
  children,
  className,
}: {
  children: React.ReactNode;
  className?: string;
}) {
  return <tbody className={cn("divide-y divide-border", className)}>{children}</tbody>;
}

export function TR({
  children,
  selected = false,
  hover = true,
  className,
  ...props
}: React.HTMLAttributes<HTMLTableRowElement> & {
  selected?: boolean;
  /** Off for header rows and for tables whose rows are not row-level things. */
  hover?: boolean;
}) {
  return (
    <tr
      {...props}
      data-state={selected ? "selected" : undefined}
      className={cn(
        "transition-colors",
        selected ? "bg-accent-subtle/40" : hover && "hover:bg-row-hover",
        className,
      )}
    >
      {children}
    </tr>
  );
}

export function TH({
  children,
  width = "auto",
  align = "left",
  className,
  ...props
}: Omit<React.ThHTMLAttributes<HTMLTableCellElement>, "align" | "width"> & {
  width?: ColumnWidth;
  align?: "left" | "right";
}) {
  return (
    <th
      scope="col"
      {...props}
      className={cn(
        "px-[var(--cell-px)] py-[var(--head-py)]",
        "text-xs font-semibold uppercase tracking-wide text-text-faint",
        align === "right" ? "text-right" : "text-left",
        WIDTHS[width],
        className,
      )}
    >
      {children}
    </th>
  );
}

export function TD({
  children,
  align = "left",
  nowrap = false,
  className,
  ...props
}: Omit<React.TdHTMLAttributes<HTMLTableCellElement>, "align"> & {
  align?: "left" | "right";
  nowrap?: boolean;
}) {
  return (
    <td
      {...props}
      className={cn(
        cellPadding,
        "align-middle text-sm text-text",
        align === "right" && "text-right",
        nowrap && "whitespace-nowrap",
        className,
      )}
    >
      {children}
    </td>
  );
}

/**
 * Row actions, aligned.
 *
 * Right-aligned and hugging the row's own content rather than the table's right
 * edge — which is what `width="min"` on the surrounding `TH` buys. `gap-1`
 * because these are usually icon buttons, and icon buttons already carry their
 * own padding.
 */
export function TableActions({
  children,
  className,
}: {
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("flex items-center justify-end gap-1", className)}>{children}</div>
  );
}

/** Two lines in one cell: the thing, and the quiet identifier under it. */
export function CellStack({
  primary,
  secondary,
  className,
}: {
  primary: React.ReactNode;
  secondary?: React.ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("min-w-0", className)}>
      <div className="truncate font-medium text-text">{primary}</div>
      {secondary && <div className="mt-0.5 truncate text-xs text-text-faint">{secondary}</div>}
    </div>
  );
}
