"use client";

/**
 * Select, on Radix.
 *
 * The native `<select>` this replaces rendered the operating system's own
 * dropdown — which on Linux and Windows means a white popup with a blue
 * highlight bar, sitting on top of a dark application. No amount of CSS reaches
 * inside it; the option list is drawn by the OS, not the page.
 *
 * Radix renders the list as real DOM, so it inherits the theme, and keeps the
 * behaviour the native element gets for free and hand-rolled dropdowns usually
 * lose: type-ahead, Home/End, arrow keys with wrapping, Escape, focus return to
 * the trigger on close, `aria-activedescendant`, and correct behaviour inside a
 * dialog.
 *
 * The API takes `options` rather than `<option>` children so a caller cannot
 * accidentally pass markup Radix will not render.
 */

import * as RSelect from "@radix-ui/react-select";
import { Check, ChevronDown } from "lucide-react";
import * as React from "react";

import { cn } from "@/lib/cn";

import { controlStyles, type ControlSize } from "./control-styles";

export type SelectOption<T extends string = string> = {
  value: T;
  label: string;
  /** Second line in the list. Never shown on the trigger — it would wrap. */
  hint?: string;
  disabled?: boolean;
};

export function Select<T extends string = string>({
  value,
  onChange,
  options,
  placeholder = "Select…",
  disabled,
  className,
  size = "md",
  "aria-label": ariaLabel,
  "aria-describedby": ariaDescribedBy,
}: {
  value: T | undefined;
  onChange: (value: T) => void;
  options: readonly SelectOption<T>[];
  placeholder?: string;
  disabled?: boolean;
  className?: string;
  size?: ControlSize;
  "aria-label"?: string;
  /** Threaded by `Field` so the hint under a select is announced with it. */
  "aria-describedby"?: string;
}) {
  return (
    <RSelect.Root
      value={value}
      onValueChange={(v) => onChange(v as T)}
      disabled={disabled}
    >
      <RSelect.Trigger
        data-no-focus-ring=""
        aria-label={ariaLabel}
        aria-describedby={ariaDescribedBy}
        // Built from the same `controlStyles` as `Input`, so a select and a
        // text field standing next to each other in a form row are the same
        // height to the pixel — which they were not when this had its own
        // padding scale.
        className={cn(
          controlStyles({ size }),
          "group inline-flex items-center justify-between gap-2 text-left",
          "data-[placeholder]:text-text-faint",
          className,
        )}
      >
        <RSelect.Value placeholder={placeholder} />
        <RSelect.Icon asChild>
          <ChevronDown
            className="size-4 shrink-0 text-text-faint transition-transform group-data-[state=open]:rotate-180"
            aria-hidden
          />
        </RSelect.Icon>
      </RSelect.Trigger>

      <RSelect.Portal>
        <RSelect.Content
          position="popper"
          sideOffset={6}
          collisionPadding={8}
          // Same rule as Combobox: a trigger scrolled out of its panel must not
          // leave the option list floating over whatever replaced it.
          hideWhenDetached
          className={cn(
            "z-50 overflow-hidden rounded-lg border border-border-strong bg-surface shadow-elevated",
            // `--radix-select-trigger-width` keeps the list the width of the
            // control instead of hugging its longest option, which otherwise
            // makes the popup jump around as the selection changes. The
            // available-height cap keeps a long list near the viewport edge
            // scrolling inside itself instead of running off the page.
            "w-[var(--radix-select-trigger-width)]",
            "max-h-[min(18rem,var(--radix-select-content-available-height))]",
            "data-[state=open]:animate-in data-[state=open]:fade-in-0",
          )}
        >
          <RSelect.Viewport className="p-1">
            {options.map((option) => (
              <RSelect.Item
                key={option.value}
                value={option.value}
                disabled={option.disabled}
                className={cn(
                  "relative flex cursor-pointer select-none flex-col rounded-md px-2 py-1.5 pr-8 text-sm text-text outline-none",
                  "data-[highlighted]:bg-surface-2 data-[state=checked]:text-accent-ink",
                  "data-[disabled]:pointer-events-none data-[disabled]:opacity-45",
                )}
              >
                <RSelect.ItemText>{option.label}</RSelect.ItemText>
                {option.hint && (
                  <span className="mt-0.5 text-xs text-text-faint">{option.hint}</span>
                )}
                <RSelect.ItemIndicator className="absolute right-2 top-2">
                  <Check className="size-4" aria-hidden />
                </RSelect.ItemIndicator>
              </RSelect.Item>
            ))}
          </RSelect.Viewport>
        </RSelect.Content>
      </RSelect.Portal>
    </RSelect.Root>
  );
}
