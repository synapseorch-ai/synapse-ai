"use client";

/**
 * Checkbox, including the indeterminate state a "select all" header needs.
 *
 * `indeterminate` is not an attribute — on a native input it can only be set
 * from JavaScript on the DOM node, which is why hand-rolled tables so often
 * show a *checked* header box when only some rows are selected. Radix models it
 * as a third value (`"indeterminate"`) and sets `aria-checked="mixed"`, so the
 * visual and the announced state agree.
 */

import * as RCheckbox from "@radix-ui/react-checkbox";
import { Check, Minus } from "lucide-react";
import * as React from "react";

import { cn } from "@/lib/cn";

export type CheckedState = boolean | "indeterminate";

export function Checkbox({
  checked,
  onChange,
  label,
  id,
  disabled,
  className,
}: {
  checked: CheckedState;
  onChange: (checked: boolean) => void;
  /**
   * Required: an unlabelled checkbox in a table row is unusable by screen
   * reader. This renders no text — it becomes the control's `aria-label`.
   * Where a *visible* label is wanted, write it next to the control and tie
   * the two together with `id`.
   */
  label: string;
  /**
   * Names the control so a visible `<label htmlFor>` can point at it, which
   * also makes that text a click target for the box.
   *
   * Same gap `Label` had before it learned `htmlFor`: without this, a checkbox
   * with a caption beside it is two unrelated elements that merely look
   * associated, and the caption is dead to both the pointer and the keyboard.
   */
  id?: string;
  disabled?: boolean;
  className?: string;
}) {
  return (
    <RCheckbox.Root
      id={id}
      checked={checked}
      onCheckedChange={(next) => onChange(next === true)}
      disabled={disabled}
      aria-label={label}
      className={cn(
        "flex size-4 shrink-0 items-center justify-center rounded-md border transition-colors",
        "border-border-strong bg-surface hover:border-accent",
        "data-[state=checked]:border-accent data-[state=checked]:bg-accent",
        "data-[state=indeterminate]:border-accent data-[state=indeterminate]:bg-accent",
        "disabled:cursor-not-allowed disabled:opacity-45",
        className,
      )}
    >
      <RCheckbox.Indicator className="text-accent-fg">
        {checked === "indeterminate" ? (
          <Minus className="size-3" strokeWidth={3} aria-hidden />
        ) : (
          <Check className="size-3" strokeWidth={3} aria-hidden />
        )}
      </RCheckbox.Indicator>
    </RCheckbox.Root>
  );
}
