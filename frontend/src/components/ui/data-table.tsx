"use client";

/**
 * The table every list screen is built from: search, column sorting, row
 * selection and a bulk-action bar.
 *
 * Hand-written rather than on TanStack Table, which was the first choice and
 * then wasn't. v9 is a ground-up rewrite — a `features` array, state in
 * TanStack Store atoms, `useTable(options, selector)` — and adopting an API
 * that new, learned from its type definitions, to sort and filter arrays of a
 * few dozen rows is a dependency doing less work than the code explaining it.
 * The parts that are genuinely hard here are focus management and ARIA in the
 * menus, selects and dialogs, and those *are* on Radix.
 *
 * Worth revisiting the moment this needs pagination, virtualisation or faceted
 * filters over thousands of rows — that is the problem TanStack actually
 * solves, and by then v9 will have documentation.
 *
 * The cell chrome is not here: `Table`, `TH` and `TD` in `./table` own it, and
 * the hand-assembled tables on the server-rendered screens use the same three.
 * Before that split, this file and those screens disagreed about padding,
 * hover and header treatment.
 */

import { ArrowDown, ArrowUp, ChevronsUpDown } from "lucide-react";
import * as React from "react";

import { cn } from "@/lib/cn";

import { Checkbox, type CheckedState } from "./checkbox";
import { SearchInput } from "./input";
import {
  TBody,
  TD,
  TH,
  THead,
  TR,
  Table,
  type ColumnWidth,
  type Density,
} from "./table";

export type Column<T> = {
  id: string;
  header: React.ReactNode;
  cell: (row: T) => React.ReactNode;
  /** Supplying this makes the column sortable. */
  sortBy?: (row: T) => string | number | null | undefined;
  /**
   * Who gets the leftover width. Mark the column the row is *about* as `full`
   * and everything else `min`, or the browser spreads the slack evenly and the
   * actions drift to the far edge of a wide window.
   */
  width?: ColumnWidth;
  /** Tailwind classes for both the header cell and every body cell. */
  className?: string;
  /** Header-only classes — usually `w-*` to pin a column to an exact width. */
  headerClassName?: string;
  align?: "left" | "right";
};

type SortState = { columnId: string; direction: "asc" | "desc" } | null;

export function DataTable<T>({
  rows,
  columns,
  getRowId,
  searchKeys,
  searchPlaceholder = "Search…",
  selection,
  bulkActions,
  empty,
  noResults,
  minWidth = "48rem",
  density = "comfortable",
  defaultSort,
}: {
  rows: T[];
  columns: Column<T>[];
  getRowId: (row: T) => string;
  /** Fields the search box matches against. Omit to hide the search box. */
  searchKeys?: (row: T) => (string | null | undefined)[];
  searchPlaceholder?: string;
  selection?: { selected: string[]; onChange: (ids: string[]) => void };
  /** Rendered in the bar that replaces the search row while rows are selected. */
  bulkActions?: (selectedIds: string[]) => React.ReactNode;
  empty?: React.ReactNode;
  noResults?: React.ReactNode;
  minWidth?: string;
  density?: Density;
  defaultSort?: SortState;
}) {
  const [query, setQuery] = React.useState("");
  const [sort, setSort] = React.useState<SortState>(defaultSort ?? null);

  const filtered = React.useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q || !searchKeys) return rows;
    return rows.filter((row) =>
      searchKeys(row).some((field) => field?.toLowerCase().includes(q)),
    );
  }, [rows, query, searchKeys]);

  const sorted = React.useMemo(() => {
    if (!sort) return filtered;
    const column = columns.find((c) => c.id === sort.columnId);
    if (!column?.sortBy) return filtered;

    const direction = sort.direction === "asc" ? 1 : -1;
    // Copy first: Array.prototype.sort mutates, and `filtered` is `rows` itself
    // whenever the search box is empty — sorting would reorder the caller's
    // props in place and desync it from the server data on the next render.
    return [...filtered].sort((a, b) => {
      const av = column.sortBy!(a);
      const bv = column.sortBy!(b);
      // Empty values sort last in both directions. A row with no "last run"
      // belongs at the bottom whichever way the column is pointing, rather
      // than taking over the top half on one click.
      if (av == null && bv == null) return 0;
      if (av == null) return 1;
      if (bv == null) return -1;
      if (typeof av === "number" && typeof bv === "number") {
        return (av - bv) * direction;
      }
      return String(av).localeCompare(String(bv), undefined, { numeric: true }) * direction;
    });
  }, [filtered, sort, columns]);

  const selectedSet = React.useMemo(
    () => new Set(selection?.selected ?? []),
    [selection?.selected],
  );
  const visibleIds = sorted.map(getRowId);
  const selectedVisible = visibleIds.filter((id) => selectedSet.has(id));

  const headerChecked: CheckedState =
    selectedVisible.length === 0
      ? false
      : selectedVisible.length === visibleIds.length
        ? true
        : "indeterminate";

  function toggleAll(checked: boolean) {
    if (!selection) return;
    // Only the rows currently visible are affected — "select all" while a
    // search is active must not silently pick up rows the user cannot see, or
    // the bulk delete they press next does more than they agreed to.
    const next = new Set(selectedSet);
    for (const id of visibleIds) {
      if (checked) next.add(id);
      else next.delete(id);
    }
    selection.onChange([...next]);
  }

  function toggleRow(id: string, checked: boolean) {
    if (!selection) return;
    const next = new Set(selectedSet);
    if (checked) next.add(id);
    else next.delete(id);
    selection.onChange([...next]);
  }

  function toggleSort(column: Column<T>) {
    if (!column.sortBy) return;
    setSort((current) =>
      current?.columnId !== column.id
        ? { columnId: column.id, direction: "asc" }
        : current.direction === "asc"
          ? { columnId: column.id, direction: "desc" }
          : null,
    );
  }

  if (rows.length === 0 && empty) return <>{empty}</>;

  const hasSelection = selection && selectedVisible.length > 0;

  return (
    <div className="overflow-hidden rounded-lg border border-border bg-surface">
      {(searchKeys || hasSelection) && (
        <div className="flex min-h-14 flex-wrap items-center gap-3 border-b border-border px-4 py-2.5">
          {hasSelection ? (
            <>
              <span className="text-sm font-medium text-text">
                {selectedVisible.length} selected
              </span>
              <div className="ml-auto flex items-center gap-2">
                {bulkActions?.(selectedVisible)}
                <button
                  type="button"
                  onClick={() => selection!.onChange([])}
                  className="rounded-md px-2 py-1 text-sm text-text-muted transition-colors hover:text-text"
                >
                  Clear
                </button>
              </div>
            </>
          ) : (
            searchKeys && (
              <SearchInput
                value={query}
                onChange={setQuery}
                placeholder={searchPlaceholder}
                aria-label={searchPlaceholder}
              />
            )
          )}
        </div>
      )}

      <Table density={density} minWidth={minWidth}>
        <THead>
          <TR hover={false}>
            {selection && (
              <TH width="min" className="pr-0">
                <Checkbox
                  checked={headerChecked}
                  onChange={toggleAll}
                  label={headerChecked === true ? "Deselect all" : "Select all"}
                />
              </TH>
            )}
            {columns.map((column) => {
              const active = sort?.columnId === column.id;
              const SortIcon = !active
                ? ChevronsUpDown
                : sort!.direction === "asc"
                  ? ArrowUp
                  : ArrowDown;
              return (
                <TH
                  key={column.id}
                  width={column.width ?? "auto"}
                  align={column.align}
                  aria-sort={
                    active ? (sort!.direction === "asc" ? "ascending" : "descending") : undefined
                  }
                  className={cn(column.headerClassName)}
                >
                  {column.sortBy ? (
                    <button
                      type="button"
                      onClick={() => toggleSort(column)}
                      className={cn(
                        "inline-flex items-center gap-1.5 rounded-md transition-colors hover:text-text",
                        active && "text-text",
                      )}
                    >
                      {column.header}
                      <SortIcon
                        className={cn("size-3.5", !active && "opacity-40")}
                        aria-hidden
                      />
                    </button>
                  ) : (
                    column.header
                  )}
                </TH>
              );
            })}
          </TR>
        </THead>
        <TBody>
          {sorted.map((row) => {
            const id = getRowId(row);
            const isSelected = selectedSet.has(id);
            return (
              <TR key={id} selected={isSelected}>
                {selection && (
                  <TD className="pr-0">
                    <Checkbox
                      checked={isSelected}
                      onChange={(checked) => toggleRow(id, checked)}
                      label={`Select row ${id}`}
                    />
                  </TD>
                )}
                {columns.map((column) => (
                  <TD
                    key={column.id}
                    align={column.align}
                    // `width: "min"` shrinks the column to its content, which
                    // only holds if the content refuses to wrap — otherwise the
                    // browser takes the invitation and breaks "Needs input"
                    // across two lines to make the column narrower still.
                    nowrap={column.width === "min"}
                    className={column.className}
                  >
                    {column.cell(row)}
                  </TD>
                ))}
              </TR>
            );
          })}
        </TBody>
      </Table>

      {sorted.length === 0 && (
        <div className="px-5 py-12 text-center">
          {noResults ?? (
            <>
              <p className="text-sm font-medium text-text">No matches</p>
              <p className="mt-1.5 text-sm text-text-muted">
                Nothing here matches “{query}”.
              </p>
            </>
          )}
        </div>
      )}
    </div>
  );
}
