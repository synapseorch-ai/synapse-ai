"use client";

/**
 * Form controls.
 *
 * **`block` is the bug fix here.** An `<input>` is `inline-block`, so two of
 * them with a width that leaves room on the line sit *side by side*. That is
 * what put the orchestration description alongside the title instead of under
 * it: both were `w-full max-w-2xl` — 672px each in a 1600px column — so the
 * browser did exactly what it was asked and laid them out as a row. Every
 * control here is `block`, which makes that layout impossible to write by
 * accident.
 *
 * `"use client"`: `Field` needs `useId` to point a hint at the control it
 * describes, and `SearchInput` owns a clear button.
 */

import { Search, X } from "lucide-react";
import * as React from "react";

import { cn } from "@/lib/cn";

import { controlStyles, type ControlSize } from "./control-styles";

export function Input({
  className,
  size = "md",
  invalid,
  ...props
}: Omit<React.ComponentProps<"input">, "size"> & {
  size?: ControlSize;
  invalid?: boolean;
}) {
  return (
    <input
      {...props}
      aria-invalid={invalid || undefined}
      className={cn(controlStyles({ size, invalid }), className)}
    />
  );
}

export function Textarea({
  className,
  size = "md",
  invalid,
  ...props
}: React.ComponentProps<"textarea"> & {
  size?: ControlSize;
  invalid?: boolean;
}) {
  return (
    <textarea
      {...props}
      aria-invalid={invalid || undefined}
      className={cn(controlStyles({ size, invalid }), "resize-y", className)}
    />
  );
}

/**
 * Label, control, and the sentence underneath.
 *
 * The `<label>` wraps the control, which associates the two without either
 * needing an id — and works for the Radix select trigger too, since a `<button>`
 * is a labelable element. What it could not do is associate the *hint*: it was
 * grey text near a field, invisible to a screen reader. So the hint and error
 * get ids, and `aria-describedby` is threaded onto the child.
 *
 * The hint and error sit **outside** the `<label>`. Everything inside a label is
 * the name of its control, so a hint inside it was announced as part of the
 * name and then again as the description — "Output key Where this step's result
 * lands… Where this step's result lands…" (F30).
 *
 * **`composite`** is for a control that holds buttons of its own — a code
 * editor with Format, a markdown editor with its tabs and toolbar. Wrapped in a
 * `<label>`, the first of those buttons would become the labelled control and a
 * click on the label text would press it. A composite field is a `role="group"`
 * instead, and the label reaches the control through `aria-labelledby`, which the
 * control forwards to its editable surface.
 */
export function Field({
  label,
  hint,
  error,
  required,
  composite,
  children,
  className,
}: {
  label: string;
  /*
   * ReactNode, not string: hints routinely name a thing the user has to type
   * back — an env var, an endpoint, a header — and those want <span
   * className="font-mono"> around them. A string-only hint pushed those call
   * sites into rendering their own <p> outside the field, which is exactly how
   * a hint ends up unreachable from aria-describedby.
   */
  hint?: React.ReactNode;
  /** Replaces the hint while set, and marks the control invalid. */
  error?: string;
  required?: boolean;
  composite?: boolean;
  children: React.ReactNode;
  className?: string;
}) {
  const id = React.useId();
  const labelId = `${id}-label`;
  const hintId = hint ? `${id}-hint` : undefined;
  const errorId = error ? `${id}-error` : undefined;
  const describedBy = [errorId, errorId ? undefined : hintId].filter(Boolean).join(" ") || undefined;

  // Threaded rather than required as a prop on every call site: `<Field><Input/></Field>`
  // is the shape all twenty-odd forms already use, and this keeps it.
  const control = React.isValidElement<{
    "aria-describedby"?: string;
    "aria-labelledby"?: string;
    "aria-invalid"?: boolean;
    invalid?: boolean;
  }>(children)
    ? React.cloneElement(children, {
        "aria-describedby":
          [children.props["aria-describedby"], describedBy].filter(Boolean).join(" ") || undefined,
        ...(composite ? { "aria-labelledby": labelId } : {}),
        ...(error ? { invalid: true } : {}),
      })
    : children;

  const heading = (
    <span id={labelId} className="mb-1.5 block text-sm font-medium text-text">
      {label}
      {required && (
        <span className="ml-0.5 text-danger" aria-hidden>
          *
        </span>
      )}
    </span>
  );
  const note = error ? (
    <span id={errorId} role="alert" className="mt-1.5 block text-xs text-danger">
      {error}
    </span>
  ) : (
    hint && (
      <span id={hintId} className="mt-1.5 block text-xs leading-relaxed text-text-faint">
        {hint}
      </span>
    )
  );

  if (composite) {
    return (
      <div className={cn("block", className)} role="group" aria-labelledby={labelId}>
        {heading}
        {control}
        {note}
      </div>
    );
  }
  return (
    <div className={cn("block", className)}>
      <label className="block">
        {heading}
        {control}
      </label>
      {note}
    </div>
  );
}

/**
 * Fields side by side, with an optional action button aligned to the controls.
 *
 * Upstreamed from the cloud's staff screens: a `Field` is `block`, so two of
 * them always stack. When a row genuinely wants label-over-control columns —
 * country + state, phone + its country — this provides the flex wrapper, and
 * the `&nbsp;` spacer drops an action button to the control line rather than
 * the label line.
 */
export function FieldRow({
  children,
  action,
  className,
}: {
  children: React.ReactNode;
  action?: React.ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("flex flex-wrap items-start gap-3", className)}>
      {children}
      {action ? (
        <div>
          <span aria-hidden className="mb-1.5 block text-sm font-medium">
            &nbsp;
          </span>
          {action}
        </div>
      ) : null}
    </div>
  );
}

/**
 * The search box that sits above a list.
 *
 * The icon, its padding offset and the clear button were assembled inline in
 * `DataTable`. Extracted so the filter box on a hand-written screen is the same
 * control, down to where the X lands.
 */
export function SearchInput({
  value,
  onChange,
  placeholder = "Search…",
  className,
  ...props
}: Omit<React.InputHTMLAttributes<HTMLInputElement>, "onChange" | "value" | "size"> & {
  value: string;
  onChange: (value: string) => void;
}) {
  return (
    <div className={cn("relative w-full max-w-xs", className)}>
      <Search
        className="pointer-events-none absolute left-2.5 top-1/2 size-4 -translate-y-1/2 text-text-faint"
        aria-hidden
      />
      <Input
        type="search"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        aria-label={props["aria-label"] ?? placeholder}
        {...props}
        // `[&::-webkit-search-cancel-button]:hidden` — WebKit draws its own X
        // on `type="search"`, which would sit beside ours.
        className="py-1.5 pl-8 pr-8 [&::-webkit-search-cancel-button]:hidden"
      />
      {value && (
        <button
          type="button"
          onClick={() => onChange("")}
          aria-label="Clear search"
          className="absolute right-2 top-1/2 -translate-y-1/2 rounded-md p-0.5 text-text-faint transition-colors hover:text-text focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring)]"
        >
          <X className="size-3.5" aria-hidden />
        </button>
      )}
    </div>
  );
}
