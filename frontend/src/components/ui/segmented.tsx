"use client";

import * as React from "react";

import { cn } from "@/lib/cn";

/**
 * A form's choice between a few values, as one segmented control (M2h, Naveen
 * at review): a value, so not the runs list's dark filter pills. A radio
 * group: the arrow keys move and choose, Tab enters on the chosen one.
 *
 * `hint` on an option is its tooltip; the page shows the chosen option's hint
 * under the control when it wants the words visible.
 */
export function Segmented<T extends string>({
  value,
  onChange,
  options,
  label,
  id,
  className,
}: {
  value: T;
  onChange: (v: T) => void;
  options: { value: T; label: string; icon?: React.ReactNode; hint?: string }[];
  /** The group's accessible name. */
  label: string;
  /** Prefix for each option's id; defaults to the label. */
  id?: string;
  className?: string;
}) {
  const prefix = `choice-${id ?? label}`;
  const move = (step: number) => {
    const i = options.findIndex((o) => o.value === value);
    const next = options[(i + step + options.length) % options.length];
    onChange(next.value);
    requestAnimationFrame(() => document.getElementById(`${prefix}-${next.value}`)?.focus());
  };
  return (
    <div
      role="radiogroup"
      aria-label={label}
      className={cn(
        "inline-flex w-fit max-w-full flex-wrap items-center gap-0.5 rounded-lg border border-border bg-surface-2 p-0.5 [:root[data-theme=dark]_&]:bg-bg",
        className,
      )}
      onKeyDown={(e) => {
        if (e.key === "ArrowRight" || e.key === "ArrowDown") {
          e.preventDefault();
          move(1);
        }
        if (e.key === "ArrowLeft" || e.key === "ArrowUp") {
          e.preventDefault();
          move(-1);
        }
      }}
    >
      {options.map((o) => {
        const on = value === o.value;
        return (
          <button
            key={o.value}
            id={`${prefix}-${o.value}`}
            type="button"
            role="radio"
            aria-checked={on}
            tabIndex={on ? 0 : -1}
            title={o.hint}
            onClick={() => onChange(o.value)}
            className={cn(
              "inline-flex h-7 items-center gap-1.5 rounded-md px-3 text-[13px] font-medium transition-colors",
              on
                ? "bg-bg text-text shadow-[0_1px_2px_rgb(16_24_40/0.12)] ring-1 ring-border-strong [:root[data-theme=dark]_&]:bg-surface-2"
                : "text-text-muted hover:text-text",
            )}
          >
            {o.icon}
            {o.label}
          </button>
        );
      })}
    </div>
  );
}
