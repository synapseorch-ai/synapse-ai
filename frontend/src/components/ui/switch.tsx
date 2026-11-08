"use client";

/**
 * A switch: on or off, taking effect as part of the form it sits in.
 *
 * A native `<button role="switch">`, not a Radix primitive: the whole
 * interaction is Space or Enter on a focused button, which the platform does
 * already, and a native button follows a `<fieldset disabled>` around it (cloud's
 * viewers see their editor that way). Five screens drew their own before this
 * one existed; new code takes this.
 *
 * Use a Checkbox where there is a third, mixed state (a group's "all of these"):
 * a switch has none.
 */

import * as React from "react";

import { cn } from "@/lib/cn";

export function Switch({
  checked,
  onChange,
  label,
  id,
  disabled,
  size = "md",
  className,
  ...aria
}: {
  checked: boolean;
  onChange: (checked: boolean) => void;
  /**
   * Required, as with Checkbox: it becomes the control's `aria-label`. Where a
   * visible label names it instead, pass `aria-labelledby` as well and the
   * visible words win.
   */
  label: string;
  id?: string;
  disabled?: boolean;
  /** `sm` for a dense row of switches (a list of tools). */
  size?: "sm" | "md";
  className?: string;
  "aria-describedby"?: string;
  "aria-labelledby"?: string;
}) {
  const sm = size === "sm";
  return (
    <button
      type="button"
      role="switch"
      id={id}
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      {...aria}
      className={cn(
        "relative inline-flex shrink-0 items-center rounded-full transition-colors",
        sm ? "h-4 w-7" : "h-5 w-9",
        checked ? "bg-accent" : "bg-border-strong",
        "disabled:cursor-not-allowed disabled:opacity-45",
        className,
      )}
    >
      <span
        aria-hidden
        className={cn(
          "inline-block rounded-full bg-surface shadow-sm transition-transform",
          sm ? "size-3" : "size-4",
          checked ? (sm ? "translate-x-3.5" : "translate-x-[18px]") : "translate-x-0.5",
        )}
      />
    </button>
  );
}
