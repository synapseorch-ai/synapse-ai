"use client";

/**
 * ⌘K: go anywhere, run anything (UX1, M1a).
 *
 * A dialog with one field and one list. Typing filters every group at once;
 * the arrow keys walk the list, Enter runs the highlighted item, Escape or a
 * click outside closes it. The list is the caller's — pages with the path to
 * them ("In Runs › Logs"), actions, things by name — so the kit knows nothing
 * of either product's navigation.
 *
 * On Radix's dialog for the focus trap, the scrim and the portal, with cmdk
 * inside it for the filtering and the keyboard (cmdk is already the engine of
 * `Combobox`). Focus goes back to whatever held it when the palette opened —
 * Radix would hand it to a trigger, and the shortcut has none (F310, as in
 * `Modal`).
 */

import * as RDialog from "@radix-ui/react-dialog";
import { Command } from "cmdk";
import { Search } from "lucide-react";
import * as React from "react";

import { cn } from "@/lib/cn";

export type CommandItem = {
  /** Unique across the palette. */
  id: string;
  label: string;
  /** A second line: where a page lives ("In Runs › Logs"), or what an action does. */
  hint?: string;
  icon?: React.ReactNode;
  /** More words it should be found by. */
  keywords?: string[];
  onSelect: () => void;
  /**
   * Makes the item draggable as well: dropping it somewhere that reads
   * `type` (the open canvas reads a step type) does what dropping it there
   * means. The palette closes once the drag has begun, so the drop reaches the
   * page under it (a modal dialog keeps the page from pointer events).
   */
  drag?: { type: string; value: string };
  /** Shown, with its `hint` saying why, but neither picked nor dragged (F882: a step the plan lacks). */
  disabled?: boolean;
};

export type CommandGroup = { heading: string; items: CommandItem[] };

/**
 * Words, not letters in order: every word typed must appear in the item's name,
 * hint or keywords. cmdk's default fuzzy score found "invoice" in "Variables —
 * In Settings › Workspace". An item whose name starts with the first word ranks
 * first, then one whose name holds it, then the rest.
 */
function filterByWords(_value: string, search: string, keywords?: string[]): number {
  const words = search.toLowerCase().split(/\s+/).filter(Boolean);
  if (words.length === 0) return 1;
  const [label = "", ...rest] = (keywords ?? []).map((k) => k.toLowerCase());
  const haystack = [label, ...rest].join(" ");
  if (!words.every((word) => haystack.includes(word))) return 0;
  if (label.startsWith(words[0]) || label.split(/\s+/).some((w) => w.startsWith(words[0]))) return 1;
  return label.includes(words[0]) ? 0.8 : 0.5;
}

const isMac = () =>
  typeof navigator !== "undefined" && /Mac|iPhone|iPad|iPod/.test(navigator.platform || navigator.userAgent);

/**
 * Opens and closes the palette from the keyboard: ⌘K on a Mac, Ctrl+K
 * elsewhere (on a Mac, Ctrl+K deletes to the end of the line in a text field).
 * A key a focused control has already handled is left alone — the markdown
 * editor's Ctrl+K still makes a link.
 */
export function useCommandShortcut(onToggle: () => void) {
  const latest = React.useRef(onToggle);
  React.useEffect(() => {
    latest.current = onToggle;
  });
  React.useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.defaultPrevented || event.isComposing || event.repeat) return;
      if (event.key.toLowerCase() !== "k" || event.shiftKey || event.altKey) return;
      const mac = isMac();
      if (mac ? !event.metaKey || event.ctrlKey : !event.ctrlKey || event.metaKey) return;
      event.preventDefault();
      latest.current();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
}

/** The shortcut as this keyboard writes it: "⌘K" or "Ctrl K". The server renders "Ctrl K". */
export function useCommandShortcutLabel(): string {
  return React.useSyncExternalStore(
    () => () => {},
    () => (isMac() ? "⌘K" : "Ctrl K"),
    () => "Ctrl K",
  );
}

export function CommandPalette({
  open,
  onOpenChange,
  groups,
  placeholder = "Jump to a page, or run something…",
  label = "Jump to",
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  groups: CommandGroup[];
  placeholder?: string;
  /** Names the dialog for a screen reader. */
  label?: string;
}) {
  const openerRef = React.useRef<HTMLElement | null>(null);
  const [query, setQuery] = React.useState("");

  return (
    <RDialog.Root
      open={open}
      onOpenChange={(next) => {
        if (!next) setQuery("");
        onOpenChange(next);
      }}
    >
      <RDialog.Portal>
        <RDialog.Overlay className="fixed inset-0 z-50 bg-scrim/40" />
        <RDialog.Content
          aria-describedby={undefined}
          onOpenAutoFocus={() => {
            const held = document.activeElement;
            openerRef.current = held instanceof HTMLElement && held !== document.body ? held : null;
          }}
          onCloseAutoFocus={(event) => {
            const opener = openerRef.current;
            openerRef.current = null;
            if (opener && opener.isConnected) {
              event.preventDefault();
              opener.focus();
            }
          }}
          className={cn(
            "fixed left-1/2 top-[max(12vh,env(safe-area-inset-top))] z-50 w-[min(620px,calc(100%-32px))] -translate-x-1/2",
            "overflow-hidden rounded-xl border border-border-strong bg-surface text-text shadow-elevated",
          )}
        >
          <RDialog.Title className="sr-only">{label}</RDialog.Title>
          {/* `vimBindings` off: cmdk's Ctrl+J/K/N/P would swallow Ctrl+K inside the
              list, and Ctrl+K is how the palette closes again. The arrows walk it. */}
          <Command loop label={label} filter={filterByWords} vimBindings={false}>
            <div className="flex items-center gap-2.5 border-b border-border px-3.5">
              <Search className="size-4 shrink-0 text-text-faint" aria-hidden />
              <Command.Input
                value={query}
                onValueChange={setQuery}
                placeholder={placeholder}
                // The global outline would ring the whole field inside a dialog that is the field.
                data-no-focus-ring
                className="h-12 min-w-0 flex-1 bg-transparent text-[15px] text-text outline-none placeholder:text-text-faint"
              />
              <kbd className="shrink-0 rounded border border-border px-1.5 font-mono text-2xs text-text-faint">esc</kbd>
            </div>
            <Command.List className="max-h-[min(60vh,440px)] overflow-y-auto overscroll-contain p-2">
              <Command.Empty className="px-3 py-8 text-center text-sm text-text-faint">
                Nothing matches “{query}”.
              </Command.Empty>
              {groups.map((group) =>
                group.items.length === 0 ? null : (
                  <Command.Group
                    key={group.heading}
                    heading={group.heading}
                    className="[&_[cmdk-group-heading]]:px-2.5 [&_[cmdk-group-heading]]:pb-1 [&_[cmdk-group-heading]]:pt-2 [&_[cmdk-group-heading]]:text-2xs [&_[cmdk-group-heading]]:font-semibold [&_[cmdk-group-heading]]:uppercase [&_[cmdk-group-heading]]:tracking-wider [&_[cmdk-group-heading]]:text-text-faint"
                  >
                    {group.items.map((item) => (
                      <Command.Item
                        key={item.id}
                        value={item.id}
                        keywords={[item.label, item.hint ?? "", ...(item.keywords ?? [])]}
                        disabled={item.disabled}
                        onSelect={() => {
                          setQuery("");
                          onOpenChange(false);
                          item.onSelect();
                        }}
                        {...(item.drag && !item.disabled
                          ? {
                              draggable: true,
                              onDragStart: (event: React.DragEvent) => {
                                event.dataTransfer.setData(item.drag!.type, item.drag!.value);
                                event.dataTransfer.effectAllowed = "move";
                                // Close on the next tick: the drag has its picture by
                                // then, and Chromium carries it on to the drop.
                                window.setTimeout(() => {
                                  setQuery("");
                                  onOpenChange(false);
                                }, 0);
                              },
                            }
                          : {})}
                        className={cn(
                          "group grid cursor-pointer grid-cols-[1.75rem_minmax(0,1fr)_auto] items-center gap-2.5 rounded-lg px-2.5 py-2 text-sm",
                          "data-[selected=true]:bg-surface-2",
                          item.drag && !item.disabled && "active:cursor-grabbing",
                          "data-[disabled=true]:cursor-not-allowed data-[disabled=true]:opacity-60",
                        )}
                      >
                        <span className="grid size-7 place-items-center rounded-md bg-surface-2 text-text-muted group-data-[selected=true]:bg-bg">
                          {item.icon}
                        </span>
                        <span className="min-w-0">
                          <span className="block truncate text-text">{item.label}</span>
                          {item.hint && <span className="block truncate text-xs text-text-faint">{item.hint}</span>}
                        </span>
                        <kbd className="invisible rounded border border-border px-1.5 font-mono text-2xs text-text-faint group-data-[selected=true]:visible">
                          ↵
                        </kbd>
                      </Command.Item>
                    ))}
                  </Command.Group>
                ),
              )}
            </Command.List>
          </Command>
        </RDialog.Content>
      </RDialog.Portal>
    </RDialog.Root>
  );
}
