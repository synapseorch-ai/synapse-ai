"use client";

/*
 * The slim rail on the right of a tabbed page (TABS1, F926): History, Pinned
 * and Search as icons, each opening one drawer. They used to be a History
 * button in the page's header; the strip took the header's place.
 *
 * Product-neutral: the panels' content is the product's, since each lists its
 * own conversations its own way.
 */

import { History, Pin, Search } from "lucide-react";
import * as React from "react";

import { Drawer } from "@/components/ui";
import { cn } from "@/lib/cn";

export type RailPanel = "history" | "pinned" | "search";

const PANELS: { id: RailPanel; label: string; title: string; icon: typeof History }[] = [
  { id: "history", label: "History", title: "History", icon: History },
  { id: "pinned", label: "Pinned", title: "Pinned", icon: Pin },
  { id: "search", label: "Search conversations", title: "Search", icon: Search },
];

export function SideRail({
  open,
  onOpen,
  render,
  descriptions,
  tour,
  className,
}: {
  open: RailPanel | null;
  onOpen: (panel: RailPanel | null) => void;
  render: (panel: RailPanel) => React.ReactNode;
  /** One faint line under a panel's title. */
  descriptions?: Partial<Record<RailPanel, React.ReactNode>>;
  tour?: string;
  className?: string;
}) {
  const shown = PANELS.find((p) => p.id === open);
  return (
    <>
      <nav aria-label="Conversations" data-tour={tour} className={cn("flex flex-col items-center gap-1 py-2", className)}>
        {PANELS.map(({ id, label, icon: Icon }) => (
          <button
            key={id}
            type="button"
            aria-label={label}
            aria-pressed={open === id}
            title={label}
            onClick={() => onOpen(open === id ? null : id)}
            className={cn(
              "grid size-8 place-items-center rounded-md text-text-faint hover:bg-surface-2 hover:text-text",
              open === id && "bg-accent-subtle text-accent-ink hover:bg-accent-subtle hover:text-accent-ink",
            )}
          >
            <Icon className="size-4" aria-hidden />
          </button>
        ))}
      </nav>
      <Drawer
        open={!!shown}
        onClose={() => onOpen(null)}
        title={shown?.title ?? ""}
        description={open ? descriptions?.[open] : undefined}
      >
        {open && render(open)}
      </Drawer>
    </>
  );
}
