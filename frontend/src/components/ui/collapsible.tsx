"use client";

/**
 * A collapsible panel section.
 *
 * The builder's Shared state and Guardrails panels were always open, which cost
 * the canvas roughly a third of the viewport to show two fields most people set
 * once. Collapsed by default, with a summary in the header so the panel still
 * answers "is anything configured here?" without being opened.
 *
 * On Radix so the closed content is genuinely removed from the accessibility
 * tree and the tab order, rather than hidden with CSS and still reachable by
 * Tab — which is what makes a keyboard user fall into an invisible form.
 */

import * as RCollapsible from "@radix-ui/react-collapsible";
import { ChevronRight } from "lucide-react";
import * as React from "react";

import { cn, focusRing } from "@/lib/cn";

import { sectionClass } from "./text";

export function Section({
  title,
  description,
  summary,
  defaultOpen = false,
  variant = "card",
  collapsible = true,
  actions,
  id,
  children,
  className,
}: {
  title: string;
  description?: string;
  /** Shown on the collapsed header — what is inside, without opening it. */
  summary?: React.ReactNode;
  defaultOpen?: boolean;
  /**
   * `card` (the default) is a bordered box, for a panel's sections. `plain` is a
   * headed group on the page itself: a section head (15px) over a hairline, no
   * box, so a page of sections is not boxes inside boxes (UX1, M2e).
   */
  variant?: "card" | "plain";
  /**
   * `false` keeps the section open with no chevron: a settings form's group,
   * whose fields must never sit behind a click (D204, M2g). The head is the same,
   * so a page of fixed and folding sections reads as one scale.
   */
  collapsible?: boolean;
  /** The head's right-hand side (a fixed section only): one button, a count. */
  actions?: React.ReactNode;
  id?: string;
  children: React.ReactNode;
  className?: string;
}) {
  const [open, setOpen] = React.useState(defaultOpen);
  const plain = variant === "plain";

  if (!collapsible) {
    return (
      <section
        id={id}
        className={cn(plain ? "mb-0 border-t border-border" : "rounded-lg border border-border bg-surface", className)}
      >
        <header
          className={cn(
            // No wrap: a long description must not push the actions onto a line of their own (M2g).
            "flex items-start justify-between gap-x-4",
            plain ? "pb-3 pt-5" : "border-b border-border px-5 py-3.5",
          )}
        >
          <div className="min-w-0 flex-1">
            <h2 className={cn("text-text", plain ? sectionClass : "text-sm font-semibold")}>{title}</h2>
            {description && <p className="mt-0.5 text-xs text-text-muted">{description}</p>}
          </div>
          {actions && <div className="flex shrink-0 flex-wrap items-center justify-end gap-2">{actions}</div>}
        </header>
        <div className={plain ? "pb-5" : "px-5 py-4"}>{children}</div>
      </section>
    );
  }

  return (
    <RCollapsible.Root
      id={id}
      open={open}
      onOpenChange={setOpen}
      className={cn(plain ? "mb-0 border-t border-border" : "rounded-lg border border-border bg-surface", className)}
    >
      <RCollapsible.Trigger
        className={cn(
          "flex w-full items-center gap-3 text-left transition-colors",
          plain ? "group rounded-md pb-3 pt-5" : "px-5 py-3.5 hover:bg-surface-2/50",
          focusRing,
          !plain && (open ? "rounded-t-lg" : "rounded-lg"),
        )}
      >
        <ChevronRight
          className={cn(
            "size-4 shrink-0 text-text-faint transition-[transform,color]",
            plain && "group-hover:text-text",
            open && "rotate-90",
          )}
          aria-hidden
        />
        <span className="min-w-0 flex-1">
          <span className={cn("block text-text", plain ? cn(sectionClass, "transition-colors group-hover:text-accent-ink") : "text-sm font-semibold")}>{title}</span>
          {description && (
            <span className="mt-0.5 block text-xs text-text-muted">{description}</span>
          )}
        </span>
        {summary && !open && (
          <span className="shrink-0 text-xs text-text-faint tabular-nums">{summary}</span>
        )}
      </RCollapsible.Trigger>

      <RCollapsible.Content>
        <div className={plain ? "pb-5" : "border-t border-border px-5 py-4"}>{children}</div>
      </RCollapsible.Content>
    </RCollapsible.Root>
  );
}
