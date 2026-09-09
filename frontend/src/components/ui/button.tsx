/**
 * Buttons, and the two things that are buttons in every way except the element.
 *
 * `Button` is the `<button>`; `LinkButton` is a navigation that looks like one;
 * `IconButton` is the square, label-less form used in table rows and panel
 * headers. All three share `buttonStyles` so a row of them lines up on the
 * pixel — before this, the icon button existed in four hand-written copies
 * (the row menu trigger, the dialog close, the run controls, the copy button)
 * at three different sizes.
 *
 * No `"use client"`: none of this holds state, so it renders on the server when
 * a server component asks for it and folds into the client bundle when a client
 * component does.
 */

import Link from "next/link";
import * as React from "react";

import { cn, focusRing } from "@/lib/cn";

import { Spinner } from "./brand";

export type ButtonVariant = "primary" | "secondary" | "danger" | "ghost";
export type ButtonSize = "sm" | "md";

const VARIANTS: Record<ButtonVariant, string> = {
  primary: "border-transparent bg-accent text-accent-fg hover:bg-accent-hover active:bg-accent",
  secondary:
    "border-border-strong bg-surface text-text hover:border-text-faint hover:bg-surface-2 active:bg-surface",
  danger: "border-transparent bg-danger text-white hover:opacity-90 active:opacity-100",
  ghost:
    "border-transparent bg-transparent text-text-muted hover:bg-surface-2 hover:text-text active:bg-surface",
};

const SIZES: Record<ButtonSize, string> = {
  sm: "gap-1.5 px-2.5 py-1.5 text-xs",
  md: "gap-2 px-3.5 py-2 text-sm",
};

/** Square, so a label-less button is not a lopsided rectangle. */
const ICON_SIZES: Record<ButtonSize, string> = {
  sm: "size-7",
  md: "size-9",
};

export function buttonStyles({
  variant = "primary",
  size = "md",
  iconOnly = false,
}: {
  variant?: ButtonVariant;
  size?: ButtonSize;
  iconOnly?: boolean;
} = {}): string {
  return cn(
    "inline-flex shrink-0 select-none items-center justify-center whitespace-nowrap rounded-md border font-medium",
    "transition-colors",
    // `aria-disabled` as well as `:disabled` — a link cannot be disabled, so a
    // `LinkButton` that should not be followed says so the only way it can.
    "disabled:pointer-events-none disabled:opacity-55 aria-disabled:pointer-events-none aria-disabled:opacity-55",
    focusRing,
    iconOnly ? ICON_SIZES[size] : SIZES[size],
    VARIANTS[variant],
  );
}

export function Button({
  children,
  variant = "primary",
  size = "md",
  loading = false,
  iconOnly = false,
  className,
  ...props
}: React.ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: ButtonVariant;
  size?: ButtonSize;
  loading?: boolean;
  iconOnly?: boolean;
}) {
  return (
    <button
      // `type` defaults to "submit" inside a form, which is how a "Cancel"
      // button ends up submitting one. Callers that mean submit say so.
      type="button"
      {...props}
      disabled={props.disabled || loading}
      className={cn(buttonStyles({ variant, size, iconOnly }), className)}
    >
      {loading && <Spinner size={16} className="shrink-0" />}
      {children}
    </button>
  );
}

/**
 * A link wearing the button's clothes.
 *
 * Four screens hand-wrote these styles onto an `<a>`, which is how "Open chat"
 * and "Build an orchestration" ended up a pixel apart from every real button
 * next to them.
 */
export function LinkButton({
  children,
  href,
  variant = "secondary",
  size = "md",
  iconOnly = false,
  external = false,
  className,
  ...props
}: Omit<React.ComponentPropsWithoutRef<typeof Link>, "href"> & {
  href: string;
  variant?: ButtonVariant;
  size?: ButtonSize;
  iconOnly?: boolean;
  /** Renders a plain `<a>` — for another subdomain, where the router cannot help. */
  external?: boolean;
}) {
  const classes = cn(buttonStyles({ variant, size, iconOnly }), className);

  if (external) {
    return (
      <a href={href} className={classes} {...(props as React.AnchorHTMLAttributes<HTMLAnchorElement>)}>
        {children}
      </a>
    );
  }

  return (
    <Link href={href} className={classes} {...props}>
      {children}
    </Link>
  );
}

/**
 * The square icon button.
 *
 * `label` is required and not optional-with-a-default: an icon-only control
 * with no accessible name is announced as "button", which in a table of them
 * means a screen reader user hears the same word six times per row.
 *
 * **And it is shown on hover, not only announced.** `aria-label` served the
 * screen-reader case and left everyone else guessing at a row of three
 * unlabelled glyphs. `title` carries the same string, so the two cannot drift.
 *
 * Deliberately the native tooltip and not `Hint`, which was tried and reverted:
 * `Hint` has to wrap its child in a focusable `inline-flex` span — that is what
 * lets it explain a *disabled* control, whose `pointer-events: none` would
 * otherwise swallow the hover. Around an enabled icon button that wrapper buys
 * nothing and costs a layout box and a second tab stop on every one of them,
 * which is visible as soon as several sit in a rail. A caller that needs the
 * disabled case can still wrap in `Hint` itself.
 */
export function IconButton({
  label,
  icon: Icon,
  tone = "default",
  size = "md",
  className,
  ...props
}: Omit<React.ButtonHTMLAttributes<HTMLButtonElement>, "children"> & {
  label: string;
  icon: React.ComponentType<{ className?: string }>;
  tone?: "default" | "danger";
  size?: ButtonSize;
}) {
  return (
    <button
      type="button"
      aria-label={label}
      // Before the spread, so an explicit `title` still wins.
      title={label}
      {...props}
      className={cn(
        buttonStyles({ variant: "ghost", size, iconOnly: true }),
        tone === "danger" && "text-danger hover:bg-danger-subtle hover:text-danger",
        // Lighter than the shared `disabled:opacity-55`: these sit in rows of
        // three, and at that size a disabled icon needs to read as off at a
        // glance rather than as a slightly dimmer live control.
        //
        // `pointer-events-none` (inherited from `buttonStyles`) is load-bearing
        // rather than cosmetic: a disabled `<button>` dispatches no mouse events
        // and they do not bubble, so the only way `Hint` can explain *why* a
        // control is disabled is for the pointer to fall through to its wrapper.
        "disabled:opacity-35",
        className,
      )}
    >
      <Icon className={size === "sm" ? "size-3.5" : "size-4"} />
    </button>
  );
}
