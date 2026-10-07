"use client";

/**
 * A hub's tab bar (D209, M1a): the pages that share one item in the menu —
 * Runs · Logs · Evals · Usage, say — under the hub's one title.
 *
 * Each tab is a page with its own address, so each is a link and the current
 * one is `aria-current="page"`, not an ARIA tab: a screen reader announces a
 * place to go, the back button steps between them, and a tab can be opened in
 * a new window. It stands on the page frame's header rule (`PageFrame`'s
 * `tabs`), and on a phone it scrolls sideways with the current tab in view.
 *
 * It takes words and addresses, not a nav entry — each product brings its own
 * hubs, and its own guard against leaving a page mid-work (`onNavigate`).
 */

import Link from "next/link";
import * as React from "react";

import { cn } from "@/lib/cn";

export type HubTab = { href: string; label: string };

export function HubTabs({
  label,
  tabs,
  current,
  onNavigate,
}: {
  /** The hub's name, which names the bar for a screen reader. */
  label: string;
  tabs: HubTab[];
  /** The address of the page being shown. */
  current: string;
  /** Called on a plain click, before navigating; `preventDefault` it to take the navigation over. */
  onNavigate?: (event: React.MouseEvent, href: string) => void;
}) {
  const currentRef = React.useRef<HTMLAnchorElement>(null);
  // On a phone the bar is wider than the screen; keep the page you are on in view.
  React.useEffect(() => {
    currentRef.current?.scrollIntoView({ block: "nearest", inline: "nearest" });
  }, [current]);

  return (
    <nav
      aria-label={label}
      // `-ml-3`: the first label's text starts on the title's line, not its padding.
      className="-mb-px -ml-3 flex gap-1 overflow-x-auto [scrollbar-width:none]"
    >
      {tabs.map((tab) => {
        const here = tab.href === current;
        return (
          <Link
            key={tab.href}
            ref={here ? currentRef : undefined}
            href={tab.href}
            aria-current={here ? "page" : undefined}
            onClick={onNavigate ? (event) => onNavigate(event, tab.href) : undefined}
            className={cn(
              "relative shrink-0 whitespace-nowrap rounded-t-md px-3 pb-2.5 pt-1.5 text-sm font-medium transition-colors",
              here ? "text-text" : "text-text-muted hover:bg-surface-2 hover:text-text",
            )}
          >
            {tab.label}
            {here && <span aria-hidden className="absolute inset-x-3 bottom-0 h-0.5 rounded-full bg-text" />}
          </Link>
        );
      })}
    </nav>
  );
}
