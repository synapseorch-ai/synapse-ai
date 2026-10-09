"use client";

import { AlertCircle, AlertTriangle, CheckCircle2, UserRound } from "lucide-react";
import * as React from "react";

import { cn } from "@/lib/cn";

import { useIsCanvasMode } from "./canvas-mode";

/**
 * Transient confirmation.
 *
 * `role="status"` rather than `"alert"`: this is for "Saved", and an assertive
 * live region interrupts whatever a screen reader is mid-sentence on. Errors
 * belong in `ErrorNote`, next to the thing that failed, where they persist.
 *
 * `warning` exists because the settings screens have a third outcome that is
 * neither: "Config saved. Use Retry to reconnect." is a success that did not
 * fully succeed, and collapsing it into either of the other two loses the only
 * part the user has to act on.
 *
 * It floats, so it sits on the surface with the elevated shadow, and the tone
 * is carried by its icon (UX1): success stays quiet, a warning is a caveat in
 * ordinary text with a warning sign, and only a failure is coloured through.
 *
 * `person` is the one warm toast (UX1: amber marks a person): a run that needs
 * someone — an approval, a question — on the person wash with the person's
 * mark, so it never reads as a caveat (M2h).
 */
export type ToastTone = "success" | "warning" | "danger" | "person";

const TOAST_TONES: Record<ToastTone, { chip: string; icon: typeof CheckCircle2; mark: string }> = {
  success: { chip: "border-border bg-surface text-text", icon: CheckCircle2, mark: "text-success" },
  warning: { chip: "border-border bg-surface text-text", icon: AlertTriangle, mark: "text-text" },
  danger: { chip: "border-danger/40 bg-danger-subtle text-danger", icon: AlertCircle, mark: "text-danger" },
  person: { chip: "border-person/50 bg-person-subtle text-text", icon: UserRound, mark: "text-person-ink" },
};

/** A tone's ground and mark, for a toast host of its own (OSS's notification stack). */
export function toastLook(tone: ToastTone) {
  return TOAST_TONES[tone];
}

export function Toast({
  message,
  tone = "success",
  onDismiss,
}: {
  message: string;
  tone?: ToastTone;
  onDismiss: () => void;
}) {
  React.useEffect(() => {
    if (!message) return;
    const timer = setTimeout(onDismiss, 4000);
    return () => clearTimeout(timer);
  }, [message, onDismiss]);
  // On a canvas the bottom row is the canvas's (the dock, the run strip, zoom):
  // the toast comes down from the top, centred under the bar, as the shell's
  // notification toasts do there (M3a).
  const canvas = useIsCanvasMode();

  if (!message) return null;

  const { chip, icon: Icon, mark } = TOAST_TONES[tone];

  return (
    <div
      role="status"
      className={cn(
        // `bottom-20`, not `bottom-5`: high enough to clear a composer's Send at
        // the bottom of chat (the corner launcher it once cleared is retired, M2c).
        "fixed z-50 flex items-center gap-2.5 rounded-lg border px-4 py-2.5 text-sm shadow-elevated",
        canvas ? "left-1/2 top-[104px] -translate-x-1/2 md:top-[70px]" : "bottom-20 right-5",
        chip,
      )}
    >
      <Icon className={cn("size-4 shrink-0", mark)} aria-hidden />
      {message}
    </div>
  );
}

/** Drives `Toast` without every page re-declaring the same two state hooks. */
export function useToast() {
  const [toast, setToast] = React.useState<{ message: string; tone: ToastTone }>({
    message: "",
    tone: "success",
  });
  const dismiss = React.useCallback(() => setToast((t) => ({ ...t, message: "" })), []);
  const show = React.useCallback(
    (message: string, tone: ToastTone = "success") => setToast({ message, tone }),
    [],
  );
  return { toast, show, dismiss };
}
