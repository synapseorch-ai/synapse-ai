"use client";

/**
 * The row overflow menu — the `⋮` at the end of a table row.
 *
 * On Radix rather than a `useState` popover, for the parts that are invisible
 * until they are missing: it closes on outside click *and* on scroll, returns
 * focus to the trigger, traps arrow-key navigation, supports type-ahead, and
 * portals out of the table so `overflow-x: auto` on the scroll container cannot
 * clip it. That last one is the specific reason a hand-rolled menu inside a
 * scrollable table nearly always ends up half-hidden.
 */

import * as RMenu from "@radix-ui/react-dropdown-menu";
import { ExternalLink, MoreVertical } from "lucide-react";
import Link from "next/link";
import * as React from "react";

import { cn } from "@/lib/cn";

import { buttonStyles } from "./button";

/* ── An item's action runs after the menu has closed ────────────────────────
 *
 * A pick closes the menu (U58: `MenuItem` used to `preventDefault` Radix's
 * select, which left every kit menu open until Escape). Radix hands focus back
 * to the trigger as the menu goes, in a timeout of its own; an action run
 * before that (a dialog opening) would take focus first and then lose it, and
 * the dialog would have nothing to return focus to when it closes. So the item
 * only records its action, and the menu runs it once the trigger has focus.
 * ────────────────────────────────────────────────────────────────────────── */

const RunAfterClose = React.createContext<((action: () => void) => void) | null>(null);

function useRunAfterClose() {
  const pendingRef = React.useRef<(() => void) | null>(null);
  const runAfterClose = React.useCallback((action: () => void) => {
    pendingRef.current = action;
  }, []);
  const onCloseAutoFocus = React.useCallback(() => {
    const run = pendingRef.current;
    pendingRef.current = null;
    // Radix focuses the trigger after this handler returns.
    if (run) setTimeout(run, 0);
  }, []);
  return { runAfterClose, onCloseAutoFocus };
}

export function RowMenu({
  children,
  label = "More actions",
}: {
  children: React.ReactNode;
  label?: string;
}) {
  const { runAfterClose, onCloseAutoFocus } = useRunAfterClose();
  return (
    <RMenu.Root>
      <RMenu.Trigger
        aria-label={label}
        className={cn(
          buttonStyles({ variant: "ghost", iconOnly: true }),
          "text-text-faint data-[state=open]:bg-surface-2 data-[state=open]:text-text",
        )}
      >
        <MoreVertical className="size-4" aria-hidden />
      </RMenu.Trigger>
      <RMenu.Portal>
        <RMenu.Content
          align="end"
          sideOffset={4}
          onCloseAutoFocus={onCloseAutoFocus}
          className="z-50 min-w-44 overflow-hidden rounded-lg border border-border-strong bg-surface p-1 shadow-xl"
        >
          <RunAfterClose.Provider value={runAfterClose}>{children}</RunAfterClose.Provider>
        </RMenu.Content>
      </RMenu.Portal>
    </RMenu.Root>
  );
}

export function MenuItem({
  children,
  onSelect,
  icon: Icon,
  tone = "default",
  disabled,
}: {
  children: React.ReactNode;
  onSelect: () => void;
  icon?: React.ComponentType<{ className?: string }>;
  tone?: "default" | "danger";
  disabled?: boolean;
}) {
  const runAfterClose = React.useContext(RunAfterClose);
  return (
    <RMenu.Item
      disabled={disabled}
      // The menu closes, then runs the action (see `useRunAfterClose`).
      onSelect={() => {
        if (runAfterClose) runAfterClose(onSelect);
        else setTimeout(onSelect, 0);
      }}
      className={cn(
        "flex cursor-pointer select-none items-center gap-2.5 rounded-md px-2 py-1.5 text-sm outline-none",
        "data-[disabled]:pointer-events-none data-[disabled]:opacity-45",
        tone === "danger"
          ? "text-danger data-[highlighted]:bg-danger-subtle"
          : "text-text data-[highlighted]:bg-surface-2",
      )}
    >
      {Icon && <Icon className="size-4 shrink-0" aria-hidden />}
      {children}
    </RMenu.Item>
  );
}

export function MenuSeparator() {
  return <RMenu.Separator className="my-1 h-px bg-border" />;
}

export function MenuLabel({ children }: { children: React.ReactNode }) {
  return (
    <RMenu.Label className="px-2 py-1.5 text-xs font-medium text-text-faint">
      {children}
    </RMenu.Label>
  );
}

/* ── A menu on any trigger ───────────────────────────────────────────────────
 *
 * The rail's account menu and the workspace switcher are the same object as a
 * row's `⋮`: a Radix dropdown that portals, traps arrow keys and returns focus.
 * What differs is only the trigger, so `Menu` takes one as a child instead of
 * drawing its own, and the items below are the shapes a shell needs that a
 * table row never does — links, and a row of choices that does not close the
 * menu when one is picked.
 * ────────────────────────────────────────────────────────────────────────── */

const ITEM =
  "relative flex cursor-pointer select-none items-center gap-2.5 rounded-md px-2 py-1.5 text-sm text-text outline-none data-[highlighted]:bg-surface-2 data-[disabled]:pointer-events-none data-[disabled]:opacity-45";

export function Menu({
  trigger,
  children,
  align = "start",
  side = "bottom",
  sideOffset = 6,
  className,
}: {
  /** Any element that can hold a ref — a plain button, a `Link`, a row. */
  trigger: React.ReactElement;
  children: React.ReactNode;
  align?: "start" | "center" | "end";
  side?: "top" | "bottom" | "left" | "right";
  sideOffset?: number;
  className?: string;
}) {
  const { runAfterClose, onCloseAutoFocus } = useRunAfterClose();
  return (
    <RMenu.Root>
      <RMenu.Trigger asChild>{trigger}</RMenu.Trigger>
      <RMenu.Portal>
        <RMenu.Content
          align={align}
          side={side}
          sideOffset={sideOffset}
          collisionPadding={8}
          onCloseAutoFocus={onCloseAutoFocus}
          className={cn(
            "z-50 min-w-56 overflow-hidden rounded-lg border border-border-strong bg-surface p-1 shadow-xl",
            className,
          )}
        >
          <RunAfterClose.Provider value={runAfterClose}>{children}</RunAfterClose.Provider>
        </RMenu.Content>
      </RMenu.Portal>
    </RMenu.Root>
  );
}

/**
 * An item that navigates. Internal links go through `Link` so prefetch and
 * modifier clicks work; external ones open a new tab and say so with the icon.
 */
export function MenuLink({
  href,
  children,
  icon: Icon,
  external = false,
  hint,
}: {
  href: string;
  children: React.ReactNode;
  icon?: React.ComponentType<{ className?: string }>;
  external?: boolean;
  /** Right-aligned, faint: a role, a shortcut, a count. */
  hint?: React.ReactNode;
}) {
  const body = (
    <>
      {Icon && <Icon className="size-4 shrink-0 text-text-muted" aria-hidden />}
      <span className="min-w-0 flex-1 truncate">{children}</span>
      {hint && <span className="shrink-0 text-xs text-text-faint">{hint}</span>}
      {external && <ExternalLink className="size-3.5 shrink-0 text-text-faint" aria-hidden />}
    </>
  );
  return (
    <RMenu.Item asChild>
      {external ? (
        <a href={href} target="_blank" rel="noopener noreferrer" className={ITEM}>
          {body}
        </a>
      ) : (
        <Link href={href} className={ITEM}>
          {body}
        </Link>
      )}
    </RMenu.Item>
  );
}

/**
 * A row of choices inside the menu — a theme picker, a density switch. Not a
 * Radix item on purpose: picking one must not close the menu, and the arrow
 * keys should keep walking the items around it rather than stopping here.
 */
export function MenuSegment<T extends string>({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: T;
  options: { value: T; label: string; icon: React.ComponentType<{ className?: string }> }[];
  onChange: (value: T) => void;
}) {
  return (
    <div className="flex items-center justify-between gap-3 px-2 py-1.5 text-sm text-text">
      <span>{label}</span>
      <div
        role="radiogroup"
        aria-label={label}
        className="flex items-center gap-0.5 rounded-md border border-border bg-bg p-0.5"
      >
        {options.map((o) => {
          const Icon = o.icon;
          const on = o.value === value;
          return (
            <button
              key={o.value}
              type="button"
              role="radio"
              aria-checked={on}
              title={o.label}
              aria-label={o.label}
              onClick={() => onChange(o.value)}
              className={cn(
                "flex size-6 items-center justify-center rounded-[calc(var(--ui-radius)-2px)] transition-colors",
                on ? "bg-surface-2 text-text" : "text-text-faint hover:text-text",
              )}
            >
              <Icon className="size-3.5" aria-hidden />
            </button>
          );
        })}
      </div>
    </div>
  );
}
