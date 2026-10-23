"use client";

/**
 * A side drawer, on Radix — the detail panel that slides in from the right.
 *
 * `Modal`'s behaviour in a different shape: focus moves in on open and back to
 * whatever held it on close, Tab is trapped inside, Escape and the backdrop
 * close it, and the page behind is `aria-hidden`. A closed drawer is not in the
 * page at all, so nothing in it can be tabbed into or read while it is out of
 * sight (the hand-rolled drawers kept their controls in reach: F179).
 *
 * "Whatever held it" is ours, as in `Modal`: Radix hands focus back only to a
 * `Dialog.Trigger`, every caller opens a drawer from state, and focus fell to
 * `<body>` on every close (F382, `Modal`'s F310 missed here).
 *
 * It slides in from the right with `@starting-style` — no keyframes, so no
 * stylesheet either product would have to carry — and appears in place under
 * reduced motion. Closing is immediate.
 *
 * Product-neutral: it takes a title, an optional description, content and a
 * footer, and knows nothing about what it shows.
 */

import * as RDialog from "@radix-ui/react-dialog";
import { X } from "lucide-react";
import * as React from "react";

import { cn } from "@/lib/cn";

import { buttonStyles } from "./button";

export function Drawer({
  open,
  onClose,
  title,
  description,
  children,
  footer,
  width = "md",
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  description?: React.ReactNode;
  children?: React.ReactNode;
  footer?: React.ReactNode;
  width?: "md" | "lg";
}) {
  // What held focus when the drawer opened, given it back on close.
  const openerRef = React.useRef<HTMLElement | null>(null);
  return (
    <RDialog.Root open={open} onOpenChange={(next) => !next && onClose()}>
      <RDialog.Portal>
        <RDialog.Overlay className="fixed inset-0 z-50 bg-scrim/40" />
        <RDialog.Content
          // Its own form submits only itself, as a Modal's does (F890): React carries a submit up its tree, out of
          // the portal and into whatever form opened it.
          onSubmit={(event) => event.stopPropagation()}
          onOpenAutoFocus={() => {
            // Called before focus moves in, so the element holding it opened us.
            const held = document.activeElement;
            openerRef.current = held instanceof HTMLElement && held !== document.body ? held : null;
          }}
          onCloseAutoFocus={(event) => {
            const opener = openerRef.current;
            openerRef.current = null;
            // What opened it may be gone by now (a deleted row's button): then
            // Radix's own behaviour stands.
            if (opener?.isConnected) {
              event.preventDefault();
              opener.focus();
            }
          }}
          className={cn(
            "fixed inset-y-0 right-0 z-50 flex w-full flex-col border-l border-border-strong bg-surface shadow-elevated",
            // Above `md` it sits inside the app's own edges, as the page's sheet
            // does (8px in, the sheet's radius), not against the window (M1a).
            "md:inset-y-2 md:right-2 md:overflow-hidden md:rounded-2xl md:border",
            "focus:outline-none",
            "transition-transform duration-200 ease-out starting:translate-x-full motion-reduce:transition-none",
            width === "md" ? "sm:max-w-lg" : "sm:max-w-2xl",
          )}
        >
          <header className="flex items-start justify-between gap-4 border-b border-border px-5 py-4">
            <div className="min-w-0">
              <RDialog.Title className="truncate text-sm font-semibold text-text">{title}</RDialog.Title>
              {description ? (
                <RDialog.Description className="mt-1 text-sm text-text-muted">{description}</RDialog.Description>
              ) : (
                <RDialog.Description className="sr-only">{title}</RDialog.Description>
              )}
            </div>
            <RDialog.Close
              aria-label="Close"
              className={cn(buttonStyles({ variant: "ghost", size: "sm", iconOnly: true }), "-mr-1.5 -mt-1 text-text-faint")}
            >
              <X className="size-4" aria-hidden />
            </RDialog.Close>
          </header>
          <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">{children}</div>
          {footer && <footer className="flex justify-end gap-2 border-t border-border px-5 py-4">{footer}</footer>}
        </RDialog.Content>
      </RDialog.Portal>
    </RDialog.Root>
  );
}
