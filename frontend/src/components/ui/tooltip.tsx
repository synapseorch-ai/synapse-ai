"use client";

/**
 * Tooltip.
 *
 * Mostly here to explain *disabled* controls, which is the case a `title`
 * attribute handles worst: a disabled button fires no pointer events, so the
 * native tooltip never appears on the one control whose reason you most need.
 * `Hint` wraps the child in a focusable span so the explanation is reachable by
 * hover, by keyboard, and by touch.
 *
 * `Provider` is mounted once in the org layout rather than per tooltip — nested
 * providers each keep their own open-delay timer, so a page with several would
 * lose the "second tooltip opens instantly" behaviour that makes a row of icon
 * buttons feel responsive rather than sticky.
 */

import * as RTooltip from "@radix-ui/react-tooltip";
import * as React from "react";

export function TooltipProvider({ children }: { children: React.ReactNode }) {
  return (
    <RTooltip.Provider delayDuration={350} skipDelayDuration={300}>
      {children}
    </RTooltip.Provider>
  );
}

export function Hint({
  children,
  content,
  side = "top",
}: {
  children: React.ReactNode;
  content: React.ReactNode;
  side?: "top" | "right" | "bottom" | "left";
}) {
  if (!content) return <>{children}</>;

  return (
    <RTooltip.Root>
      {/*
        A disabled button swallows pointer events, so the trigger has to be a
        wrapper around it rather than the button itself — otherwise the tooltip
        never fires in exactly the state it exists to explain. `tabIndex={0}`
        keeps it keyboard-reachable for the same reason.
      */}
      <RTooltip.Trigger asChild>
        {/*
          `has-[:disabled]` puts the not-allowed cursor on the wrapper, because
          the disabled control inside has `pointer-events: none` — which is what
          lets the hover reach this span at all — and so cannot show a cursor of
          its own.
        */}
        <span
          tabIndex={0}
          className="inline-flex outline-none has-[:disabled]:cursor-not-allowed"
        >
          {children}
        </span>
      </RTooltip.Trigger>
      <RTooltip.Portal>
        <RTooltip.Content
          side={side}
          sideOffset={6}
          collisionPadding={8}
          className="z-50 max-w-64 rounded-md border border-border-strong bg-surface px-2.5 py-1.5 text-xs leading-relaxed text-text shadow-elevated"
        >
          {content}
          <RTooltip.Arrow className="fill-[var(--border-strong)]" />
        </RTooltip.Content>
      </RTooltip.Portal>
    </RTooltip.Root>
  );
}
