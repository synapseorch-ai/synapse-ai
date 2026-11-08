"use client";

/**
 * A one-line `Input` that suggests `{{ … }}` names as you type them.
 *
 * Everything `Input` takes, plus the field's `profile` — which names it can
 * render, see `template-complete`. Without a profile it is a plain `Input`.
 * Monospace, because what goes in these fields is a URL or a header value that
 * someone has to read character by character.
 */

import * as React from "react";

import { cn } from "@/lib/cn";

import { Input } from "./input";
import type { TemplateProfile } from "./template-complete";
import { useSuggestions } from "./template-suggest";

export function TemplateInput({
  profile,
  value,
  className,
  onKeyDown,
  onSelect,
  onBlur,
  ...props
}: Omit<React.ComponentProps<typeof Input>, "value"> & {
  value: string;
  profile?: TemplateProfile;
}) {
  const ref = React.useRef<HTMLInputElement>(null);
  const suggest = useSuggestions({ ref, value, profile, disabled: props.disabled || props.readOnly });

  return (
    <>
      <Input
        {...props}
        {...suggest.aria}
        ref={ref}
        value={value}
        spellCheck={false}
        autoComplete="off"
        className={cn("font-code text-xs", className)}
        onChange={(event) => {
          suggest.track(event);
          props.onChange?.(event);
        }}
        onSelect={(event) => {
          suggest.track(event);
          onSelect?.(event);
        }}
        onKeyDown={(event) => {
          suggest.onKeyDown(event);
          if (!event.defaultPrevented) onKeyDown?.(event);
        }}
        onBlur={(event) => {
          suggest.onBlur();
          onBlur?.(event);
        }}
      />
      {suggest.list}
    </>
  );
}
