"use client";

import { AlertCircle, AlertTriangle, CheckCircle2 } from "lucide-react";
import * as React from "react";

import { cn } from "@/lib/cn";

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
 * part the user has to act on. The tokens were already there.
 */
export type ToastTone = "success" | "warning" | "danger";

const TOAST_TONES: Record<ToastTone, { chip: string; icon: typeof CheckCircle2 }> = {
  success: { chip: "border-success/40 bg-success-subtle text-success", icon: CheckCircle2 },
  warning: { chip: "border-warning/40 bg-warning-subtle text-warning", icon: AlertTriangle },
  danger: { chip: "border-danger/40 bg-danger-subtle text-danger", icon: AlertCircle },
};

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

  if (!message) return null;

  const { chip, icon: Icon } = TOAST_TONES[tone];

  return (
    <div
      role="status"
      className={cn(
        // `bottom-20`, not `bottom-5`: the AI builder's launcher owns the
        // bottom-right corner in both products, and a toast landing on top of
        // it hides the control the user is most likely reaching for.
        "fixed bottom-20 right-5 z-50 flex items-center gap-2.5 rounded-lg border px-4 py-2.5 text-sm shadow-xl",
        chip,
      )}
    >
      <Icon className="size-4 shrink-0" aria-hidden />
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
