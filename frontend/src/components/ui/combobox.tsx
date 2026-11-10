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

import { AppMark } from "./app-mark";
import { useOwnScroll } from "./own-scroll";

import { controlStyles, type ControlSize } from "./control-styles";
import type { SelectOption } from "./select";

export type ComboboxOption<T extends string = string> = SelectOption<T> & {
  /** Renders under a heading, the way `<optgroup>` did. */
  group?: string;
  /**
   * Whose it is, as a small mark on the right of its row and of the closed field: a model's provider (PICK1,
   * F1482). Decoration beside a name that says it in words, so it is hidden from a screen reader as `AppMark` is.
   */
  mark?: OptionMark;
};

/** A mark an option wears: a logo of ours, else `AppMark`'s letter tile from `name`. */
export type OptionMark = { name: string; src?: string; className?: string };

/**
 * What typing matches: the label, the id, its group and its mark's name, so "anthropic" finds Claude's models
 * whether they sit under Anthropic or under a group of their own, as cloud's managed models do (PICK1, F1482).
 */
function searchText(option: ComboboxOption<string>): string {
  const words = [option.label, option.value, option.group, option.mark?.name].filter(Boolean);
  return [...new Set(words)].join(" ");
}

/** A text's words: its runs of letters and digits, in lower case ("openrouter.anthropic/claude-opus-4.5" → openrouter,
 *  anthropic, claude, opus, 4, 5). */
function wordsOf(text: string): string[] {
  return text.toLowerCase().split(/[^\p{L}\p{N}]+/u).filter(Boolean);
}

/**
 * How well what was typed finds an option, for cmdk (F1493): 1 when every word typed begins one of the option's
 * words ("opus", "gpt oss", "sonnet 4.6"), ½ when what was typed is inside its text whole ("pus"), else 0, which
 * hides it. cmdk's own score took any of the letters in order, across the label, the id and the group: "opus" listed
 * GPT OSS Safeguard (o-ss … p … u … s), Llama 4 Scout and Claude Sonnet 4.6. Equal scores keep the list's order.
 */
export function matchScore(text: string, typed: string): number {
  const wanted = wordsOf(typed);
  if (!wanted.length) return 1;
  const own = wordsOf(text);
  if (wanted.every((w) => own.some((o) => o.startsWith(w)))) return 1;
  const plain = (s: string) => s.toLowerCase().replace(/\s+/g, " ").trim();
  return plain(text).includes(plain(typed)) ? 0.5 : 0;
}

function Mark({ mark }: { mark: OptionMark }) {
  return (
    <span data-option-mark={mark.name} className="flex shrink-0">
      <AppMark name={mark.name} imgSrc={mark.src} imgClassName={mark.className} size="xs" />
    </span>
  );
}

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
  trigger = "field",
  leading,
  iconOnPhone = false,
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
  /**
   * `field` (the default) shows the choice in a select-shaped box. `icon` is a
   * square chevron beside something that already shows the choice — the open
   * canvas's name — and its list takes a fixed width. Name it with `aria-label`.
   * `pill` is a rounded chip that shows the choice inside a composer's bar (the
   * agent, the builder's model); its list takes the same fixed width.
   */
  trigger?: "field" | "icon" | "pill";
  /** Drawn before the choice in a `pill` trigger: the agent's tile, say. */
  leading?: React.ReactNode;
  /**
   * A `pill` that is its `leading` tile and chevron below `md`, its choice kept for a screen reader and on hover:
   * Chat's agent picker, whose row a phone gives to the model's whole label (F1171).
   */
  iconOnPhone?: boolean;
  "aria-label"?: string;
  "aria-describedby"?: string;
}) {
  const [open, setOpen] = React.useState(false);
  // What was typed, held here to rank the groups by it (below). Each opening starts it empty, as cmdk's own did.
  const [search, setSearch] = React.useState("");
  const selected = options.find((o) => o.value === value);
  // Portalled out of any dialog it opens in: without this a Modal's or a Drawer's scroll lock threw away
  // every wheel and swipe over it (F700, `own-scroll.ts`).
  const ownScroll = useOwnScroll<HTMLDivElement>();

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

  // cmdk ranks the options inside a group by `matchScore`, but 1.1.1 never moves a group: it looks a group up by its
  // id, and the page carries the group's heading. So a group goes first here when its best option does, and groups
  // that score the same keep their order: "mini" lists OpenAI's gpt-5-mini before the Gemini models it is only
  // inside of (F1493).
  const ranked = React.useMemo(() => {
    if (!search) return groups;
    const best = (items: ComboboxOption<T>[]) => Math.max(...items.map((o) => matchScore(searchText(o), search)));
    return groups
      .map((group) => [group, best(group[1])] as const)
      .sort((a, b) => b[1] - a[1])
      .map(([group]) => group);
  }, [groups, search]);

  return (
    <RPopover.Root
      open={open}
      onOpenChange={(next) => {
        if (next) setSearch("");
        setOpen(next);
      }}
    >
      <RPopover.Trigger
        data-no-focus-ring=""
        disabled={disabled}
        aria-label={ariaLabel}
        aria-describedby={ariaDescribedBy}
        // A pill truncates its label: the whole of it on hover (F1131).
        title={trigger === "pill" ? (selected?.label ?? placeholder) : undefined}
        className={cn(
          controlStyles({ size }),
          "group inline-flex items-center justify-between gap-2 text-left",
          !selected && "text-text-faint",
          trigger === "icon" && "size-8 w-8 shrink-0 justify-center border-transparent bg-transparent px-0 py-0 hover:bg-surface-2",
          trigger === "pill" && cn(
            "h-7 w-auto min-w-0 max-w-full shrink justify-start gap-1.5 rounded-full border-transparent bg-surface-2 py-0 text-xs font-medium",
            leading ? "pl-1 pr-2" : "px-2.5",
            iconOnPhone && "max-md:pr-1.5",
            "hover:border-transparent hover:bg-row-hover",
          ),
          className,
        )}
      >
        {trigger === "pill" && leading}
        <span className={trigger === "icon" ? "sr-only" : cn("truncate", trigger === "pill" && iconOnPhone && "max-md:sr-only")}>
          {selected?.label ?? placeholder}
        </span>
        {trigger === "field" && selected?.mark && (
          <span className="ml-auto flex shrink-0">
            <Mark mark={selected.mark} />
          </span>
        )}
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
            "z-50 flex flex-col overflow-hidden rounded-lg border border-border-strong bg-surface shadow-elevated",
            // Match the trigger's width rather than hugging the longest option,
            // which otherwise makes the popup jump as the selection changes. An
            // icon trigger is too narrow to match, so its list has a width of its own.
            trigger === "field" ? "w-[var(--radix-popover-trigger-width)]" : "w-72 max-w-[calc(100vw-16px)]",
            // Never taller than the space Radix measured to the viewport edge —
            // without this a long list near the bottom of the screen ran off
            // the page instead of scrolling inside itself.
            "max-h-[var(--radix-popover-content-available-height)]",
          )}
        >
          {/* `shouldFilter` on: cmdk scores each item's `value`, which is
              `searchText`, with `matchScore`: typing the start of a word of its
              name, its id or its provider finds it (F1493). */}
          <Command loop filter={matchScore} className="flex min-h-0 flex-col">
            <div className="flex items-center gap-2 border-b border-border px-3">
              <Search className="size-4 shrink-0 text-text-faint" aria-hidden />
              <Command.Input
                value={search}
                onValueChange={setSearch}
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

            <Command.List ref={ownScroll} className="max-h-72 min-h-0 overflow-y-auto overscroll-contain p-1">
              <Command.Empty className="px-2 py-6 text-center text-sm text-text-faint">
                {emptyMessage}
              </Command.Empty>

              {ranked.map(([group, items]) => (
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
                      value={searchText(option)}
                      disabled={option.disabled}
                      onSelect={() => {
                        onChange(option.value);
                        setOpen(false);
                      }}
                      className={cn(
                        "relative flex cursor-pointer select-none flex-col rounded-md px-2 py-1.5 text-sm text-text outline-none",
                        option.mark ? "pr-16" : "pr-8",
                        "data-[selected=true]:bg-surface-2",
                        "data-[disabled=true]:pointer-events-none data-[disabled=true]:opacity-45",
                        option.value === value && "text-accent-ink",
                      )}
                    >
                      <span className="truncate">{option.label}</span>
                      {option.hint && (
                        <span className="mt-0.5 text-xs text-text-faint">{option.hint}</span>
                      )}
                      {option.value === value && (
                        <Check className={cn("absolute top-2 size-4", option.mark ? "right-9" : "right-2")} aria-hidden />
                      )}
                      {option.mark && (
                        <span className="absolute right-2 top-1.5">
                          <Mark mark={option.mark} />
                        </span>
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
