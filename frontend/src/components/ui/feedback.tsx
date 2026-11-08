/**
 * The three ways a screen says "nothing here", "that worked" and "that didn't".
 */

import { AlertCircle, CheckCircle2 } from "lucide-react";
import * as React from "react";

import { cn } from "@/lib/cn";

/** Inline error. `role="alert"` so screen readers announce it on appearance. */
export function ErrorNote({
  children,
  className,
}: {
  children: React.ReactNode;
  className?: string;
}) {
  if (!children) return null;
  return (
    <div
      role="alert"
      className={cn(
        "flex items-start gap-2 rounded-md border border-danger/25 bg-danger-subtle px-3 py-2.5 text-sm leading-relaxed text-danger",
        className,
      )}
    >
      <AlertCircle className="mt-0.5 size-4 shrink-0" aria-hidden />
      <span className="min-w-0">{children}</span>
    </div>
  );
}

/**
 * Inline confirmation. `aria-live="polite"` so it is announced when it appears.
 *
 * Polite rather than `role="alert"`, which its counterpart above uses: an error
 * interrupts because it usually means the thing you asked for did not happen,
 * while "saved" can wait for a gap in whatever is being read. Both have to say
 * *something* though — a form whose only feedback is a green box is a form that
 * silently does nothing for anyone not looking at it.
 */
export function SuccessNote({
  children,
  className,
}: {
  children: React.ReactNode;
  className?: string;
}) {
  if (!children) return null;
  return (
    <div
      aria-live="polite"
      className={cn(
        "flex items-start gap-2 rounded-md border border-success/25 bg-success-subtle px-3 py-2.5 text-sm leading-relaxed text-success",
        className,
      )}
    >
      <CheckCircle2 className="mt-0.5 size-4 shrink-0" aria-hidden />
      <span className="min-w-0">{children}</span>
    </div>
  );
}

export function EmptyState({
  title,
  children,
  action,
  className,
}: {
  title: string;
  children?: React.ReactNode;
  /** One button, for the thing they would do next. */
  action?: React.ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("px-5 py-12 text-center", className)}>
      <p className="text-sm font-medium text-text">{title}</p>
      {children && (
        <p className="mx-auto mt-1.5 max-w-md text-sm leading-relaxed text-text-muted">
          {children}
        </p>
      )}
      {action && <div className="mt-4 flex justify-center gap-2">{action}</div>}
    </div>
  );
}
