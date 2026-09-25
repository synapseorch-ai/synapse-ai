"use client";

/**
 * Combobox — a Select with the search *inside* the popup.
 *
 * `Select` is the right control for a handful of options. It stops being the
 * right control at 66: the Models screen lists 37 Gemini models alone, and
 * Radix Select's type-ahead only jumps to a prefix, so finding
 * `gemini-3-flash-preview` meant scrolling a list the height of the screen.
 *
 * Why not just put an input inside `Select`: Radix Select owns every keystroke
 * for its own type-ahead, so a text field placed in its content fights it for
 * the keyboard. Filtering is a different primitive — Popover for the surface,
 * `cmdk` for the list. That is the same trade the rest of this directory makes:
 * outsource the parts that are quietly hard (filtering + `aria-activedescendant`
 * + roving focus + portalling) and keep the markup.
 *
 * The trigger is built from `controlStyles` exactly as `Select`'s is, so the
 * two are the same height in a form row and swapping one for the other is a
 * one-word change.
 */

import * as RPopover from "@radix-ui/react-popover";
import { Command } from "cmdk";
import { Check, ChevronDown, Search } from "lucide-react";
import * as React from "react";

import { cn } from "@/lib/cn";

import { controlStyles, type ControlSize } from "./control-styles";
import type { SelectOption } from "./select";

export type ComboboxOption<T extends string = string> = SelectOption<T> & {
  /** Renders under a heading, the way `<optgroup>` did. */
  group?: string;
};

export function Combobox<T extends string = string>({
  value,
  onChange,
  options,
  placeholder = "Select…",
  searchPlaceholder = "Search…",
  emptyMessage = "No matches.",
  disabled,
  className,
  size = "md",
  "aria-label": ariaLabel,
  "aria-describedby": ariaDescribedBy,
}: {
  value: T | undefined;
  onChange: (value: T) => void;
  options: readonly ComboboxOption<T>[];
  placeholder?: string;
  searchPlaceholder?: string;
  emptyMessage?: string;
  disabled?: boolean;
  className?: string;
  size?: ControlSize;
  "aria-label"?: string;
  "aria-describedby"?: string;
}) {
  const [open, setOpen] = React.useState(false);
  const selected = options.find((o) => o.value === value);

  // Grouped in source order. A Map preserves insertion order, so providers come
  // out in the order the caller listed them rather than alphabetically.
  const groups = React.useMemo(() => {
    const byGroup = new Map<string, ComboboxOption<T>[]>();
    for (const option of options) {
      const key = option.group ?? "";
      const bucket = byGroup.get(key);
      if (bucket) bucket.push(option);
      else byGroup.set(key, [option]);
    }
    return [...byGroup.entries()];
  }, [options]);

  return (
    <RPopover.Root open={open} onOpenChange={setOpen}>
      <RPopover.Trigger
        disabled={disabled}
        aria-label={ariaLabel}
        aria-describedby={ariaDescribedBy}
        className={cn(
          controlStyles({ size }),
          "group inline-flex items-center justify-between gap-2 text-left",
          !selected && "text-text-faint",
          className,
        )}
      >
        <span className="truncate">{selected?.label ?? placeholder}</span>
        <ChevronDown
          className="size-4 shrink-0 text-text-faint transition-transform group-data-[state=open]:rotate-180"
          aria-hidden
        />
      </RPopover.Trigger>

      <RPopover.Portal>
        <RPopover.Content
          align="start"
          sideOffset={6}
          collisionPadding={8}
          // The panel behind this scrolls. Without this, scrolling the trigger
          // out of view leaves the popup clamped at the viewport edge, floating
          // over unrelated content — hidden-when-detached is the honest state.
          hideWhenDetached
          className={cn(
            "z-50 flex flex-col overflow-hidden rounded-lg border border-border-strong bg-surface shadow-xl",
            // Match the trigger's width rather than hugging the longest option,
            // which otherwise makes the popup jump as the selection changes.
            "w-[var(--radix-popover-trigger-width)]",
            // Never taller than the space Radix measured to the viewport edge —
            // without this a long list near the bottom of the screen ran off
            // the page instead of scrolling inside itself.
            "max-h-[var(--radix-popover-content-available-height)]",
          )}
        >
          {/* `shouldFilter` on: cmdk scores against each item's `value`, which
              is set below to the label plus the id, so typing either finds it. */}
          <Command loop className="flex min-h-0 flex-col">
            <div className="flex items-center gap-2 border-b border-border px-3">
              <Search className="size-4 shrink-0 text-text-faint" aria-hidden />
              <Command.Input
                autoFocus
                placeholder={searchPlaceholder}
                // A placeholder is not a name: without this the field read as an
                // unnamed text box to a screen reader (F187).
                aria-label={ariaLabel ? `Search ${ariaLabel}` : "Search options"}
                // The popover is the focused surface — it has a border, a
                // shadow and a highlighted item. A ring around the field inside
                // it is a second box saying the same thing, and it is the first
                // thing you see because the field is autofocused on open.
                data-no-focus-ring
                className="h-9 w-full bg-transparent text-sm text-text outline-none placeholder:text-text-faint"
              />
            </div>

            <Command.List className="max-h-72 min-h-0 overflow-y-auto overscroll-contain p-1">
              <Command.Empty className="px-2 py-6 text-center text-sm text-text-faint">
                {emptyMessage}
              </Command.Empty>

              {groups.map(([group, items]) => (
                <Command.Group
                  key={group || "_"}
                  heading={group || undefined}
                  className={cn(
                    "[&_[cmdk-group-heading]]:px-2 [&_[cmdk-group-heading]]:pb-1 [&_[cmdk-group-heading]]:pt-2",
                    "[&_[cmdk-group-heading]]:text-2xs [&_[cmdk-group-heading]]:font-medium",
                    "[&_[cmdk-group-heading]]:uppercase [&_[cmdk-group-heading]]:tracking-wider",
                    "[&_[cmdk-group-heading]]:text-text-faint",
                  )}
                >
                  {items.map((option) => (
                    <Command.Item
                      key={option.value}
                      value={`${option.label} ${option.value}`}
                      disabled={option.disabled}
                      onSelect={() => {
                        onChange(option.value);
                        setOpen(false);
                      }}
                      className={cn(
                        "relative flex cursor-pointer select-none flex-col rounded-md px-2 py-1.5 pr-8 text-sm text-text outline-none",
                        "data-[selected=true]:bg-surface-2",
                        "data-[disabled=true]:pointer-events-none data-[disabled=true]:opacity-45",
                        option.value === value && "text-accent",
                      )}
                    >
                      <span className="truncate">{option.label}</span>
                      {option.hint && (
                        <span className="mt-0.5 text-xs text-text-faint">{option.hint}</span>
                      )}
                      {option.value === value && (
                        <Check className="absolute right-2 top-2 size-4" aria-hidden />
                      )}
                    </Command.Item>
                  ))}
                </Command.Group>
              ))}
            </Command.List>
          </Command>
        </RPopover.Content>
      </RPopover.Portal>
    </RPopover.Root>
  );
}
