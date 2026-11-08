/**
 * The shape of a form control, in a module that declares neither
 * `"use client"` nor anything React.
 *
 * That is deliberate and load-bearing. Next compiles *every* export of a
 * `"use client"` module into a client reference, so a server component can
 * render such an export as a component but never call it as a function — the
 * same trap `lib/format.ts` documents. The staff search forms are server
 * components with plain `<input>` elements, and they need these classes, so the
 * classes cannot live next to the components that also use them.
 *
 * `Input`, `Textarea` and the Radix select trigger all build on this, which is
 * what makes a select and a text field the same height when they sit in the
 * same form row.
 */

import { cn, focusRingInset } from "@/lib/cn";

export type ControlSize = "sm" | "md";

const CONTROL_SIZES: Record<ControlSize, string> = {
  sm: "px-2.5 py-1.5 text-xs",
  md: "px-3 py-2 text-sm",
};

export function controlStyles({
  size = "md",
  invalid = false,
}: { size?: ControlSize; invalid?: boolean } = {}): string {
  return cn(
    // `block` is the fix for the bug that started this: an `<input>` is
    // `inline-block`, so two of them narrow enough to share a line will do
    // exactly that — which is how the orchestration description ended up
    // beside its title rather than under it.
    "block w-full rounded-md border bg-surface text-text transition-colors",
    "placeholder:text-text-faint",
    "disabled:opacity-60",
    CONTROL_SIZES[size],
    focusRingInset,
    invalid
      ? "border-danger focus-visible:border-danger focus-visible:ring-danger-subtle"
      : "border-border-strong hover:border-text-faint",
  );
}
