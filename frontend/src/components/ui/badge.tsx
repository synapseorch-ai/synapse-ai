/**
 * Badges and tags.
 *
 * A badge states what something *is* — a role, a plan, a step type. A tag is a
 * value someone attached and can take off again.
 *
 * `whitespace-nowrap` matters more than it sounds: these sit in table cells
 * that shrink, and a two-word badge breaking across lines takes the row height
 * with it and makes one row taller than its neighbours.
 *
 * Tones follow the colour rule (UX1): `machine` is the product acting, `person`
 * is someone needed (the one loud chip), `danger` a failure. `success` is quiet
 * — neutral text with a green dot — and `warning` is a problem or a caveat, not
 * a person: neutral-strong text with a warning sign, which is what carries it.
 */

import { AlertTriangle, X } from "lucide-react";
import * as React from "react";

import { cn } from "@/lib/cn";

export type BadgeTone = "neutral" | "accent" | "machine" | "person" | "danger" | "warning" | "success";

/** `mark`: what the tone always shows before its label, whatever `dot` says. */
const TONES: Record<BadgeTone, { chip: string; dot: string; mark?: "dot" | "icon" }> = {
  neutral: { chip: "bg-surface-2 text-text-muted", dot: "bg-text-faint" },
  accent: { chip: "bg-accent-subtle text-accent-ink", dot: "bg-accent" },
  machine: { chip: "bg-machine-subtle text-accent-ink", dot: "bg-machine" },
  person: { chip: "bg-person text-person-fg", dot: "bg-person-fg" },
  danger: { chip: "bg-danger-subtle text-danger", dot: "bg-danger" },
  warning: { chip: "bg-surface-2 text-text", dot: "bg-text", mark: "icon" },
  success: { chip: "bg-surface-2 text-text-muted", dot: "bg-success", mark: "dot" },
};

export function Badge({
  children,
  tone = "neutral",
  size = "md",
  dot = false,
  className,
}: {
  children: React.ReactNode;
  tone?: BadgeTone;
  size?: "sm" | "md";
  /** A coloured dot before the label, for when the tone alone is too quiet. */
  dot?: boolean;
  className?: string;
}) {
  const mark = TONES[tone].mark ?? (dot ? "dot" : null);
  return (
    <span
      className={cn(
        "inline-flex max-w-full items-center gap-1.5 whitespace-nowrap rounded-md font-medium",
        size === "md" ? "px-2 py-0.5 text-xs" : "px-1.5 py-0.5 text-[0.6875rem]",
        TONES[tone].chip,
        className,
      )}
    >
      {mark === "icon" && <AlertTriangle className="size-3 shrink-0" aria-hidden />}
      {mark === "dot" && <span className={cn("size-1.5 shrink-0 rounded-full", TONES[tone].dot)} aria-hidden />}
      <span className="truncate">{children}</span>
    </span>
  );
}

/** A value someone attached: outlined rather than filled, and removable. */
export function Tag({
  children,
  onRemove,
  className,
}: {
  children: React.ReactNode;
  onRemove?: () => void;
  className?: string;
}) {
  return (
    <span
      className={cn(
        "inline-flex max-w-full items-center gap-1 whitespace-nowrap rounded-md border border-border-strong bg-surface py-0.5 pl-2 text-xs text-text-muted",
        onRemove ? "pr-1" : "pr-2",
        className,
      )}
    >
      <span className="truncate">{children}</span>
      {onRemove && (
        <button
          type="button"
          onClick={onRemove}
          aria-label={`Remove ${typeof children === "string" ? children : "tag"}`}
          className="rounded-md p-0.5 text-text-faint transition-colors hover:bg-surface-2 hover:text-text"
        >
          <X className="size-3" aria-hidden />
        </button>
      )}
    </span>
  );
}
