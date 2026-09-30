"use client";

/**
 * A side drawer, on Radix — the detail panel that slides in from the right.
 *
 * `Modal`'s behaviour in a different shape: focus moves in on open and back to
 * the trigger on close, Tab is trapped inside, Escape and the backdrop close
 * it, and the page behind is `aria-hidden`. A closed drawer is not in the page
 * at all, so nothing in it can be tabbed into or read while it is out of sight
 * (the hand-rolled drawers kept their controls in reach: F179).
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
  return (
    <RDialog.Root open={open} onOpenChange={(next) => !next && onClose()}>
      <RDialog.Portal>
        <RDialog.Overlay className="fixed inset-0 z-50 bg-scrim/40" />
        <RDialog.Content
          className={cn(
            "fixed inset-y-0 right-0 z-50 flex w-full flex-col border-l border-border-strong bg-surface shadow-2xl",
            "focus:outline-none",
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
