/**
 * Type, and the two blocks built purely out of it.
 *
 * Every screen opened with the same fourteen-word incantation —
 * `<h1 className="text-xl font-semibold text-text">` followed by
 * `<p className="mt-1 text-sm text-text-muted">` inside a wrapping flex row —
 * and the stat tile existed in three separate definitions at two sizes. Both
 * are one component now, so a change to the page title is a change to the page
 * title rather than to fourteen files.
 */

import * as React from "react";

import { cn } from "@/lib/cn";

import { PageHeaderSlot } from "./page-header-slot";

export type Tone = "neutral" | "accent" | "machine" | "person" | "danger" | "warning" | "success";

/** As `Badge`'s tones: the machine and person colours as their inks; a warning in ordinary strong text. */
const TONE_TEXT: Record<Tone, string> = {
  neutral: "text-text-muted",
  accent: "text-accent-ink",
  machine: "text-accent-ink",
  person: "text-person-ink",
  danger: "text-danger",
  warning: "text-text",
  success: "text-success",
};

/**
 * The title scale (UX1), as classes rather than components, so a page frame can
 * put them on the element it already renders: a page's title (28px, 600,
 * −0.028em, from Inter's display cut) and a section's head (15px, 600). The
 * sizes come from `tokens.css` through `globals.css`'s `text-title` and
 * `text-section`; `lib/cn.ts` keeps tailwind-merge from dropping them.
 */
export const titleClass = "text-title text-balance text-text";
export const sectionClass = "text-section text-text";

/**
 * The `<h1>` block at the top of a screen, with its actions on the right.
 *
 * The title is the page title of the type scale (`titleClass`, 28px; 24px on a
 * phone), since M1a — it was 20px, the same as everything else.
 *
 * The shell's slot (the help "?") leads the right-hand cluster, so the page's
 * own actions — a primary button, Settings' close — keep the far right. The
 * cluster renders even with no actions, since the slot still needs a place, and
 * hides itself when both are empty (`empty:hidden`): an empty item in a wrapping
 * row would otherwise wrap alone and add a row gap under a long title.
 */
export function PageHeader({
  title,
  description,
  actions,
  eyebrow,
  children,
  className,
}: {
  title: React.ReactNode;
  description?: React.ReactNode;
  actions?: React.ReactNode;
  /**
   * The small line above the title: where the page lives. `null` draws the
   * line empty, which is how `PageFrame` keeps every title at one height;
   * leaving it out draws no line at all.
   */
  eyebrow?: React.ReactNode;
  /** Sits above the title — a back link, a breadcrumb. */
  children?: React.ReactNode;
  className?: string;
}) {
  return (
    <div className={className}>
      {children && <div className="mb-3">{children}</div>}
      {eyebrow !== undefined && (
        <div className="mb-1 flex h-5 items-center gap-1.5 text-xs font-medium text-text-faint">{eyebrow}</div>
      )}
      {/* The actions share the title's row, not the description's: beside the
          whole text block they wrapped under a long description on a phone. */}
      <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-3">
        <h1 className={cn(titleClass, "min-w-0 max-sm:text-2xl")}>{title}</h1>
        <div className="flex shrink-0 flex-wrap items-center gap-2 empty:hidden">
          <PageHeaderSlot />
          {actions}
        </div>
      </div>
      {description && (
        <p className="mt-1.5 max-w-2xl text-sm leading-relaxed text-text-muted">{description}</p>
      )}
    </div>
  );
}

export function Heading({
  children,
  level = 2,
  className,
}: {
  children: React.ReactNode;
  level?: 2 | 3;
  className?: string;
}) {
  const Tag = level === 2 ? "h2" : "h3";
  return (
    <Tag
      className={cn(
        level === 2 ? "text-sm font-semibold text-text" : "text-xs font-medium text-text-muted",
        className,
      )}
    >
      {children}
    </Tag>
  );
}

export function Text({
  children,
  tone = "default",
  className,
}: {
  children: React.ReactNode;
  tone?: "default" | "muted" | "faint";
  className?: string;
}) {
  return (
    <p
      className={cn(
        "text-sm leading-relaxed",
        { default: "text-text", muted: "text-text-muted", faint: "text-text-faint" }[tone],
        className,
      )}
    >
      {children}
    </p>
  );
}

/**
 * The small uppercase line that labels a group.
 *
 * The single most-repeated string in the app: ~98 hand-written
 * `uppercase tracking-*` labels, most of them some permutation of
 * `text-[10px] uppercase font-bold text-zinc-500 tracking-wider` — written in
 * six different orders, at three different sizes, with two different weights,
 * because there was nothing to import.
 *
 * The shape here is not invented. `AppRail` and the settings sub-nav had both
 * already converged on `text-2xs font-medium uppercase tracking-wider
 * text-text-faint` independently, which is a good sign it is the right one.
 * `font-medium` rather than the `font-bold` most call sites carry: bold at
 * 11px with wide tracking is a smear, and the uppercase already does the
 * work of separating this from body text.
 *
 * For an ordinary stacked field, reach for `Field` instead — it wraps the
 * control, which associates the two without an id, and threads
 * `aria-describedby` at the hint.
 *
 * `htmlFor` is the escape hatch for the case `Field` cannot serve: a control
 * that shares its row with another control. The API-key rows pair an input with
 * a show/hide button, and wrapping those in one `<label>` would put a `<button>`
 * inside a label — the label then has two labelable descendants and the
 * association depends on document order. Naming the control by id says exactly
 * which one is meant. Without `htmlFor` this stays a `<span>`, so it is still
 * safe to drop inline in a flex row.
 */
export function Label({
  children,
  size = "xs",
  htmlFor,
  className,
}: {
  children: React.ReactNode;
  /** `xs` is the 11px form used inside dense panels; `sm` the 12px section eyebrow. */
  size?: "xs" | "sm";
  /** Names the control this labels. Switches the element to a real `<label>`. */
  htmlFor?: string;
  className?: string;
}) {
  const Tag = htmlFor ? "label" : "span";
  return (
    <Tag
      htmlFor={htmlFor}
      className={cn(
        "font-medium uppercase tracking-wider text-text-faint",
        size === "sm" ? "text-xs" : "text-2xs",
        className,
      )}
    >
      {children}
    </Tag>
  );
}

/** The small grey line under something — a hint, a timestamp, an id. */
export function Muted({
  children,
  className,
}: {
  children: React.ReactNode;
  className?: string;
}) {
  return <span className={cn("text-xs text-text-faint", className)}>{children}</span>;
}

/** Ids, keys, step names: anything the user may need to compare character by character. */
export function Mono({
  children,
  className,
}: {
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <span className={cn("font-mono text-xs text-text-muted", className)}>{children}</span>
  );
}

/**
 * One number on a bordered tile.
 *
 * `tone` colours the label and icon, never the number — a red *value* reads as
 * "this figure is wrong" rather than "this figure needs attention", and the
 * dashboard's success rate is one number people will screenshot.
 */
export function Stat({
  label,
  value,
  hint,
  tone = "neutral",
  icon: Icon,
  size = "md",
  className,
}: {
  label: string;
  value: React.ReactNode;
  hint?: string;
  tone?: Tone;
  icon?: React.ComponentType<{ className?: string }>;
  size?: "sm" | "md";
  className?: string;
}) {
  return (
    <div
      className={cn(
        "rounded-lg border border-border bg-surface",
        size === "md" ? "p-5" : "p-4",
        className,
      )}
    >
      <div className={cn("flex items-center gap-2", TONE_TEXT[tone])}>
        {Icon && <Icon className="size-4 shrink-0" aria-hidden />}
        <span className="text-sm">{label}</span>
      </div>
      <p
        className={cn(
          "mt-2 font-semibold tabular-nums text-text",
          size === "md" ? "text-2xl" : "text-lg",
        )}
      >
        {value}
      </p>
      {hint && <p className="mt-1 text-xs text-text-faint">{hint}</p>}
    </div>
  );
}
