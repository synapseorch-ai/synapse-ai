/**
 * Class composition for the design system.
 *
 * `clsx` alone is not enough once components accept a `className`. It
 * concatenates, and Tailwind's utilities are then resolved by the order they
 * happen to sit in the generated stylesheet — not the order they appear in the
 * string. So `<Button variant="ghost" className="text-danger">`, which is how
 * every destructive row action is written, was only red by luck: `text-danger`
 * and the variant's `text-text-muted` are both single utilities, and whichever
 * Tailwind emitted last won for every call site at once.
 *
 * `twMerge` resolves conflicts by *Tailwind's* semantics — later wins, one
 * winner per property group — so a caller's override is reliable and the
 * primitives below are safe to extend at the point of use.
 */

import { clsx, type ClassValue } from "clsx";
import { extendTailwindMerge } from "tailwind-merge";

/*
 * The scale entries `globals.css` adds to Tailwind's. tailwind-merge only knows
 * Tailwind's own names, so it read `text-title` as a text *colour* and
 * `shadow-elevated` as a shadow colour, and silently dropped one when the other
 * followed: `cn("text-section font-semibold text-text")` came back without its
 * size (measured, M0a). Named here, each sits in its real group.
 */
const twMerge = extendTailwindMerge({
  extend: {
    theme: {
      text: ["title", "section"],
      shadow: ["elevated"],
    },
  },
});

export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}

/**
 * Focus, in one place: one indicator per control, on the accent.
 *
 * There were three treatments in the codebase — `focus:ring-2`,
 * `focus-visible:ring-2`, and the global `:focus-visible { outline }` — so
 * tabbing across a form changed the shape of the indicator between controls.
 * And the kit's own ring never replaced the outline: the global rule is
 * unlayered, so it beats `focus-visible:outline-none` (a layered utility)
 * whatever the specificity, and every kit control drew both — measured, M0a.
 *
 * So the indicator is the global outline (2px of `--accent`, 2px out), which
 * every element gets on `:focus-visible` — keyboard focus, never a mouse click.
 * `focusRing` adds nothing to it; it stays as the name call sites already use.
 * A control that owns a border shows focus on that border instead
 * (`focusRingInset`) and sets `data-no-focus-ring`, which turns the outline
 * off for it. The alpha ring alone would not do: it is about 1.4:1, under the
 * 3:1 a focus indicator needs; the accent border is over 5:1.
 */
export const focusRing = "";

/** Focus for controls that own a border: the border turns accent, with a soft ring. Pair with `data-no-focus-ring`. */
export const focusRingInset = "focus-visible:border-accent focus-visible:ring-2 focus-visible:ring-[var(--ring)]";
