"use client";

/*
 * The tab strip (TABS1, F923): the open tabs, each with its state, the "N more"
 * past the strip's width, and the new tab's button at the end.
 *
 * An ARIA tablist, unlike HubTabs: these tabs are views of one page, not
 * addresses. Arrow keys move between them, × or a middle click closes one, and
 * a drag reorders them. Each tab says its state in its name, so a screen reader
 * hears "Chase the Northwind invoices, needs you".
 *
 * Product-neutral: it takes tabs and badges, and each product brings what a
 * tab shows, where tabs are saved, and its own new-tab button.
 */

import { Check, ChevronDown, X } from "lucide-react";
import * as React from "react";

import { AppMark, Menu, MenuItem, Spinner } from "@/components/ui";
import { cn } from "@/lib/cn";

import { BADGE_WORDS, splitVisible, type Tab, type TabBadge, type TabsState } from "./model";

/** A tab's width bounds, and what the strip keeps clear for "+" and "N more". */
const TAB_MIN = 132;
const TAB_GAP = 2;
const PLUS = 40;
const MORE = 92;

export interface StripTab extends Tab {
  /** The agent or workflow the tab's tile is drawn from. */
  tileName: string;
  tileSeed?: string;
}

export function TabStrip<T extends StripTab>({
  label,
  state,
  badges,
  onFocus,
  onClose,
  onMove,
  newTab,
  untitled = "New tab",
  className,
}: {
  /** Names the strip for a screen reader: "Open conversations". */
  label: string;
  state: TabsState<T>;
  badges: Record<string, TabBadge>;
  onFocus: (id: string) => void;
  onClose: (id: string) => void;
  onMove: (id: string, to: number) => void;
  /** The new tab's control, drawn after the last tab. */
  newTab: React.ReactNode;
  untitled?: string;
  className?: string;
}) {
  const boxRef = React.useRef<HTMLDivElement>(null);
  const [width, setWidth] = React.useState(0);
  React.useLayoutEffect(() => {
    const box = boxRef.current;
    if (!box) return;
    const measure = () => setWidth(box.clientWidth);
    measure();
    const seen = new ResizeObserver(measure);
    seen.observe(box);
    return () => seen.disconnect();
  }, []);

  const each = TAB_MIN + TAB_GAP;
  const allFit = Math.floor((width - PLUS) / each);
  const fits = width === 0 ? state.tabs.length
    : state.tabs.length <= allFit ? allFit : Math.floor((width - PLUS - MORE) / each);
  const { shown, more } = splitVisible(state, fits);

  const titleOf = (t: T) => t.title || untitled;
  const nameOf = (t: T) => {
    const words = BADGE_WORDS[badges[t.id] ?? "none"];
    return words ? `${titleOf(t)}, ${words}` : titleOf(t);
  };

  // Arrow keys move along the strip, as a tablist's do; Home and End go to its ends.
  const onKeyDown = (e: React.KeyboardEvent) => {
    const at = shown.findIndex((t) => t.id === state.activeId);
    const to = e.key === "ArrowRight" ? at + 1 : e.key === "ArrowLeft" ? at - 1
      : e.key === "Home" ? 0 : e.key === "End" ? shown.length - 1 : null;
    if (to === null || shown.length === 0) return;
    e.preventDefault();
    const next = shown[(to + shown.length) % shown.length];
    onFocus(next.id);
    boxRef.current?.querySelector<HTMLElement>(`[data-tab-id="${CSS.escape(next.id)}"]`)?.focus();
  };

  const [dragging, setDragging] = React.useState<string | null>(null);

  return (
    <div ref={boxRef} className={cn("flex min-w-0 flex-1 items-end gap-0.5", className)}>
      <div role="tablist" aria-label={label} onKeyDown={onKeyDown} className="flex min-w-0 items-end gap-0.5">
        {shown.map((t) => {
          const on = t.id === state.activeId;
          const badge = badges[t.id] ?? "none";
          return (
            <div
              key={t.id}
              draggable
              onDragStart={(e) => { setDragging(t.id); e.dataTransfer.effectAllowed = "move"; }}
              onDragEnd={() => setDragging(null)}
              onDragOver={(e) => { if (dragging) e.preventDefault(); }}
              onDrop={(e) => {
                e.preventDefault();
                if (dragging && dragging !== t.id) onMove(dragging, state.tabs.findIndex((x) => x.id === t.id));
                setDragging(null);
              }}
              className={cn(
                "group relative flex h-[34px] min-w-[132px] max-w-56 flex-[0_1_14rem] items-center rounded-t-lg border border-b-0 border-transparent text-text-muted",
                on ? "z-10 -mb-px h-[35px] border-border bg-bg text-text" : "hover:bg-row-hover",
                dragging === t.id && "opacity-60",
              )}
            >
              <button
                type="button"
                role="tab"
                data-tab-id={t.id}
                aria-selected={on}
                aria-label={nameOf(t)}
                tabIndex={on ? 0 : -1}
                title={titleOf(t)}
                onClick={() => onFocus(t.id)}
                // A middle click closes, as a browser's tab does.
                onAuxClick={(e) => { if (e.button === 1) { e.preventDefault(); onClose(t.id); } }}
                onMouseDown={(e) => { if (e.button === 1) e.preventDefault(); }}
                className="flex h-full min-w-0 flex-1 items-center gap-2 pl-2.5 pr-1 text-left text-[13px]"
              >
                <AppMark name={t.tileName} seed={t.tileSeed} size="sm" className="size-[18px] rounded-[5px] text-[10px]" />
                <span
                  className={cn(
                    "min-w-0 flex-1 truncate",
                    on && "font-medium text-text",
                    badge === "unread" && "font-semibold text-text",
                  )}
                >
                  {titleOf(t)}
                </span>
                <BadgeMark badge={badge} />
              </button>
              <button
                type="button"
                aria-label={`Close ${titleOf(t)}`}
                title="Close (Alt+W)"
                tabIndex={-1}
                onClick={() => onClose(t.id)}
                className={cn(
                  "mr-1.5 grid size-[22px] shrink-0 place-items-center rounded-[5px] text-text-faint hover:bg-surface-2 hover:text-text focus-visible:opacity-100",
                  on ? "opacity-100" : "opacity-0 group-hover:opacity-100",
                )}
              >
                <X className="size-3.5" aria-hidden />
              </button>
            </div>
          );
        })}
      </div>
      {more.length > 0 && (
        <Menu
          align="end"
          trigger={(
            <button
              type="button"
              className="mb-0.5 flex h-8 shrink-0 items-center gap-1 rounded-md px-2 text-[13px] text-text-muted hover:bg-surface-2 hover:text-text"
            >
              {more.length} more
              {more.some((t) => (badges[t.id] ?? "none") === "needs-you") && (
                <span role="img" aria-label="One of them needs you" className="size-2 rounded-full bg-person" />
              )}
              <ChevronDown className="size-3.5" aria-hidden />
            </button>
          )}
        >
          {more.map((t) => (
            <MenuItem key={t.id} onSelect={() => onFocus(t.id)}>
              <AppMark name={t.tileName} seed={t.tileSeed} size="sm" className="size-[18px] rounded-[5px] text-[10px]" />
              <span className="min-w-0 flex-1 truncate">{titleOf(t)}</span>
              <BadgeMark badge={badges[t.id] ?? "none"} />
            </MenuItem>
          ))}
        </Menu>
      )}
      <div className="mb-0.5 shrink-0">{newTab}</div>
    </div>
  );
}

/** One mark per state: the brand spinner while working, amber for the person, a tick for done, a dot for news. */
export function BadgeMark({ badge }: { badge: TabBadge }) {
  if (badge === "working") return <Spinner size={14} />;
  if (badge === "needs-you") return <span aria-hidden className="size-2 shrink-0 rounded-full bg-person" />;
  if (badge === "done") return <Check aria-hidden className="size-3.5 shrink-0 text-success" />;
  if (badge === "unread") return <span aria-hidden className="size-2 shrink-0 rounded-full bg-accent" />;
  return null;
}
