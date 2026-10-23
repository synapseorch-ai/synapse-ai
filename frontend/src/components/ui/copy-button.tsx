"use client";

/**
 * Copy, and "Copied" for two seconds after. Lifted from the run transcript's own
 * (F901) so the run's result and anything else that offers its text share one.
 * No clipboard (an insecure origin) leaves the text on screen to select.
 */

import { Check, Copy } from "lucide-react";
import * as React from "react";

import { Button, type ButtonSize, type ButtonVariant } from "./button";

export function CopyButton({
  text,
  label = "Copy",
  variant = "secondary",
  size = "sm",
  className,
}: {
  /** The text, or a function that makes it when pressed. */
  text: string | (() => string);
  label?: string;
  variant?: ButtonVariant;
  size?: ButtonSize;
  className?: string;
}) {
  const [copied, setCopied] = React.useState(false);
  const timer = React.useRef<ReturnType<typeof setTimeout> | null>(null);
  React.useEffect(() => () => {
    if (timer.current) clearTimeout(timer.current);
  }, []);

  async function copy() {
    const value = typeof text === "function" ? text() : text;
    if (!value) return;
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      if (timer.current) clearTimeout(timer.current);
      timer.current = setTimeout(() => setCopied(false), 2000);
    } catch {
      // No clipboard: the text is still on screen to select.
    }
  }

  return (
    <Button variant={variant} size={size} onClick={copy} className={className} aria-live="polite">
      {copied ? <Check className="size-4" aria-hidden /> : <Copy className="size-4" aria-hidden />}
      {copied ? "Copied" : label}
    </Button>
  );
}
