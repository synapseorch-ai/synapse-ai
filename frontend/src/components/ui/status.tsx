"use client";

/**
 * Run status, rendered consistently everywhere it appears.
 *
 * One map, because a status shown as amber on one screen and grey on the next
 * is how people stop trusting the colour and start reading every label.
 *
 * The colours follow the product's one rule (UX1): cyan is the machine acting,
 * amber is a person being needed, and green and red only report an outcome —
 * success quietly, as a dot beside ordinary text, because most runs succeed and
 * a page of green says nothing. The machine and person colours are fills here,
 * never text: their text is `accent-ink` and `person-ink`, which read on white.
 *
 * Colour is never the only signal: each state has its own dot treatment and its
 * own word, so the table still reads correctly in greyscale and to the ~8% of
 * men with a colour vision deficiency — for whom `failed` red and `completed`
 * green are the single most commonly confused pair.
 */

import * as React from "react";

import { cn } from "@/lib/cn";

export type RunStatus =
  | "queued"
  | "running"
  | "paused"
  | "completed"
  | "failed"
  | "cancelled";

const STATUS: Record<RunStatus, { label: string; dot: string; text: string; pulse?: boolean }> = {
  queued: { label: "Queued", dot: "bg-text-faint", text: "text-text-muted" },
  running: { label: "Running", dot: "bg-machine", text: "text-accent-ink", pulse: true },
  paused: { label: "Needs input", dot: "bg-person", text: "font-medium text-person-ink" },
  completed: { label: "Completed", dot: "bg-success", text: "text-text-muted" },
  failed: { label: "Failed", dot: "bg-danger", text: "text-danger" },
  cancelled: { label: "Cancelled", dot: "bg-text-faint", text: "text-text-faint" },
};

export function normaliseStatus(
  status: string | null | undefined,
  waitingForHuman?: boolean,
): RunStatus | null {
  if (!status) return null;
  // The engine models "waiting for a human" as a boolean alongside a status
  // that is still `running`, so without this the one state a person has to act
  // on is the one that looks like it needs nothing.
  if (waitingForHuman) return "paused";
  return status in STATUS ? (status as RunStatus) : null;
}

export function StatusBadge({
  status,
  waitingForHuman,
  fallback = "Never run",
}: {
  status: string | null | undefined;
  waitingForHuman?: boolean;
  fallback?: string;
}) {
  const key = normaliseStatus(status, waitingForHuman);

  if (!key) {
    return <span className="text-sm text-text-faint">{fallback}</span>;
  }

  const meta = STATUS[key];
  return (
    // `whitespace-nowrap`: these sit in a shrink-to-fit column, and "Needs
    // input" broken across two lines makes one table row taller than the rest.
    <span className={cn("inline-flex items-center gap-2 whitespace-nowrap text-sm", meta.text)}>
      <span className="relative flex size-2 shrink-0">
        {meta.pulse && (
          <span
            className={cn("absolute inline-flex size-full animate-ping rounded-full opacity-70", meta.dot)}
            aria-hidden
          />
        )}
        <span className={cn("relative inline-flex size-2 rounded-full", meta.dot)} aria-hidden />
      </span>
      {meta.label}
    </span>
  );
}
