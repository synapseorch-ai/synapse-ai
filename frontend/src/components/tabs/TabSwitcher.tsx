"use client";

/*
 * Tabs on a phone (TABS1, F927): no strip. One button shows the tab in front, a
 * count, and an amber dot when another tab needs the person; it opens a sheet
 * from the bottom with every tab, its state, and New tab.
 */

import * as RDialog from "@radix-ui/react-dialog";
import { Plus, X } from "lucide-react";
import * as React from "react";

import { AppMark } from "@/components/ui";
import { cn } from "@/lib/cn";

import { BadgeMark, type StripTab } from "./TabStrip";
import { BADGE_WORDS, activeTab, type TabBadge, type TabsState } from "./model";

export function TabSwitcher<T extends StripTab>({
  state,
  badges,
  onFocus,
  onClose,
  onNew,
  untitled = "New tab",
  subtitle,
  tour,
  className,
}: {
  state: TabsState<T>;
  badges: Record<string, TabBadge>;
  onFocus: (id: string) => void;
  onClose: (id: string) => void;
  /** Called after the sheet has closed: the new tab's picker opens where the person can see it. */
  onNew: () => void;
  untitled?: string;
  /** The faint line under a tab's title: its agent's name. */
  subtitle: (tab: T) => string;
  tour?: string;
  className?: string;
}) {
  const [open, setOpen] = React.useState(false);
  const front = activeTab(state);
  const count = state.tabs.length;
  const waiting = state.tabs.some((t) => t.id !== state.activeId && badges[t.id] === "needs-you");
  const titleOf = (t: T) => t.title || untitled;

  return (
    <RDialog.Root open={open} onOpenChange={setOpen}>
      <RDialog.Trigger asChild>
        <button
          type="button"
          data-tour={tour}
          aria-label={`Switch tab: ${front ? titleOf(front) : untitled}, ${count} open${waiting ? ", one needs you" : ""}`}
          className={cn(
            "flex min-h-11 w-full items-center gap-2.5 rounded-[10px] border border-border-strong bg-surface px-2.5 py-1.5 text-left",
            className,
          )}
        >
          {front && <AppMark name={front.tileName} seed={front.tileSeed} size="sm" className="size-[22px] rounded-md text-[11px]" />}
          <span className="min-w-0 flex-1">
            <span className="block truncate text-sm font-semibold text-text">{front ? titleOf(front) : untitled}</span>
            {front && <span className="block truncate text-xs text-text-faint">{subtitle(front)}</span>}
          </span>
          <span aria-hidden className="grid h-[26px] min-w-[26px] place-items-center rounded-[7px] border-[1.5px] border-text-muted px-1.5 text-xs font-bold text-text">
            {count}
          </span>
          {waiting && <span aria-hidden className="size-2 shrink-0 rounded-full bg-person" />}
        </button>
      </RDialog.Trigger>
      <RDialog.Portal>
        <RDialog.Overlay className="fixed inset-0 z-50 bg-scrim/40" />
        <RDialog.Content
          aria-describedby={undefined}
          className="fixed inset-x-0 bottom-0 z-50 flex max-h-[80dvh] flex-col gap-1 rounded-t-[18px] bg-surface px-3 pb-5 pt-2 shadow-elevated"
        >
          <span aria-hidden className="mx-auto mb-2.5 mt-1 h-1 w-10 rounded-full bg-border-strong" />
          <div className="flex items-center px-1.5 pb-1.5">
            <RDialog.Title className="flex-1 text-base font-semibold text-text">{count} open tabs</RDialog.Title>
          </div>
          <div className="custom-scrollbar -mx-1 flex flex-col gap-1 overflow-y-auto px-1">
            {state.tabs.map((t) => {
              const badge = badges[t.id] ?? "none";
              const words = BADGE_WORDS[badge];
              return (
                <div key={t.id} className={cn("flex items-center rounded-[10px]", t.id === state.activeId && "bg-surface-2")}>
                  <button
                    type="button"
                    onClick={() => { onFocus(t.id); setOpen(false); }}
                    aria-current={t.id === state.activeId ? "true" : undefined}
                    className="flex min-h-[52px] min-w-0 flex-1 items-center gap-3 px-3 py-2 text-left"
                  >
                    <AppMark name={t.tileName} seed={t.tileSeed} size="sm" className="size-[22px] rounded-md text-[11px]" />
                    <span className="min-w-0 flex-1">
                      <span className={cn("block truncate text-[15px] text-text", badge === "unread" ? "font-semibold" : "font-medium")}>
                        {titleOf(t)}
                      </span>
                      <span className="block truncate text-[12.5px] text-text-faint">
                        {subtitle(t)}{words ? ` · ${words}` : ""}
                      </span>
                    </span>
                    <BadgeMark badge={badge} />
                  </button>
                  <button
                    type="button"
                    aria-label={`Close ${titleOf(t)}`}
                    onClick={() => onClose(t.id)}
                    className="grid size-11 shrink-0 place-items-center rounded-md text-text-faint hover:bg-surface-2 hover:text-text"
                  >
                    <X className="size-4" aria-hidden />
                  </button>
                </div>
              );
            })}
          </div>
          <button
            type="button"
            onClick={() => { setOpen(false); setTimeout(onNew, 0); }}
            className="flex min-h-[52px] items-center gap-3 rounded-[10px] px-3 text-[15px] font-semibold text-accent-ink hover:bg-row-hover"
          >
            <span className="grid size-[22px] place-items-center"><Plus className="size-[18px]" aria-hidden /></span>
            New tab
          </button>
        </RDialog.Content>
      </RDialog.Portal>
    </RDialog.Root>
  );
}
