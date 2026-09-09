/**
 * The SynapseOrch mark, wordmark and loader.
 *
 * Three nested layers around a solid core. The layers are the hidden layers;
 * the gaps in them are the synapses; a signal crosses between two layers only
 * through a gap that belongs to both. That last rule is what fixes every angle
 * in the geometry — it is not a styling choice, and
 * `scripts/gen-brand.mjs --audit` fails if an edit breaks it.
 *
 * The geometry is generated, not written here: see `brand.paths.ts` and the
 * script that emits it. The loader is derived from the *same* radii and gap
 * angles as the static mark, so the logo and the animation cannot drift apart.
 *
 * DEPENDS ON `brand-motion.css`, imported by `globals.css` in both apps. That
 * is a cross-repo contract, the same shape as the one `--ui-radius` already
 * has: the class names below are inert without it.
 */

import * as React from "react";

import { cn } from "@/lib/cn";

import {
  BRAND_GRID,
  COMPACT_BELOW,
  MARK_COMPACT,
  MARK_FULL,
  MOTION,
  type MarkGeometry,
} from "./brand.paths";

function geometryFor(size: number, compact?: boolean): MarkGeometry {
  return (compact ?? size < COMPACT_BELOW) ? MARK_COMPACT : MARK_FULL;
}

function Rings({ g }: { g: MarkGeometry }) {
  return (
    <>
      {g.rings.map((d) => (
        <path
          key={d}
          d={d}
          fill="none"
          stroke="currentColor"
          strokeWidth={g.stroke}
          strokeLinecap="butt"
        />
      ))}
      {g.core.map((d) => (
        <path key={d} d={d} />
      ))}
    </>
  );
}

/**
 * The mark on its own.
 *
 * `currentColor` throughout and no background, which is what lets one component
 * serve the nav, the login card, light mode and a knocked-out print. Below
 * `COMPACT_BELOW` it swaps to the two-layer form — three layers merge into a
 * disc at that size, which was measured by rendering it rather than guessed.
 * The reduced form is the same construction with one layer dropped, so a tab
 * and the app show recognisably the same mark.
 */
export function Mark({
  size = 24,
  compact,
  className,
}: {
  size?: number;
  /** Force the reduced form. Left unset, it follows `size`. */
  compact?: boolean;
  className?: string;
}) {
  const g = geometryFor(size, compact);
  return (
    <svg
      width={size}
      height={size}
      viewBox={`0 0 ${BRAND_GRID} ${BRAND_GRID}`}
      fill="currentColor"
      aria-hidden
      focusable="false"
      className={cn("shrink-0", className)}
    >
      <Rings g={g} />
      {g.beads.at.map(([x, y]) => (
        <circle key={`${x},${y}`} cx={x} cy={y} r={g.beads.r} />
      ))}
    </svg>
  );
}

/**
 * Mark plus name.
 *
 * The name is live text, not outlined paths: this is a UI label, so it should
 * set in whatever face the surrounding interface uses — and it stays
 * selectable, translatable and legible to a screen reader. One colour, the
 * mark carries the accent; a two-tone wordmark would put the accent twice into
 * a rail that already uses it for the active item.
 */
export function Wordmark({
  size = 20,
  className,
  markClassName,
}: {
  size?: number;
  className?: string;
  markClassName?: string;
}) {
  return (
    <span className={cn("inline-flex items-center gap-2", className)}>
      <Mark size={size} className={markClassName} />
      <span className="font-semibold tracking-tight">SynapseOrch</span>
    </span>
  );
}

/**
 * The mark, working.
 *
 * Signals spawn at the outer layer, spiral inward through the gaps and are
 * absorbed by the core — a forward pass. Every signal runs the identical path
 * at the identical speed; only the release times differ, evenly spread across
 * one period so a new signal enters every `period / signals` seconds.
 *
 * Use it where work is genuinely in flight — a turn streaming, a tool call, a
 * sign-in resolving. Route-level skeletons stay skeletons: swapping one for a
 * centred spinner makes a screen feel slower, not faster.
 *
 * Under `prefers-reduced-motion` the travelling signals are replaced by parked
 * ones (see `brand-motion.css`), so the mark still reads as busy while nothing
 * moves.
 */
/**
 * The inline busy indicator: the mark, turning, with signals running it.
 *
 * Every "something is happening" in both products goes through this rather
 * than a generic spinner — a product that shows its own mark working reads as
 * one thing, and a rotating slash reads as a browser default. It is
 * `BrandLoader` with a size chosen for text: at 16px the two-layer form is
 * what survives, and `BrandLoader` picks it automatically below 20.
 *
 * Sizes worth knowing: 14 beside `text-xs`, 16 beside `text-sm` and inside a
 * button, 20 in a table cell, 40+ for an empty state — above that reach for
 * `BrandLoader` directly and give it a label.
 */
export function Spinner({
  size = 16,
  label,
  className,
}: {
  size?: number;
  /** Announce the busy state. Omit when text beside it already says so. */
  label?: string;
  className?: string;
}) {
  return <BrandLoader size={size} label={label} className={className} />;
}

export function BrandLoader({
  size = 56,
  compact,
  label,
  spin = true,
  className,
}: {
  size?: number;
  compact?: boolean;
  /**
   * Announce the busy state. Omit it when a visible line beside the loader
   * already says what is happening — two announcements of one event is worse
   * than none.
   */
  label?: string;
  /** The whole assembly turns. Off for a mark sitting beside body text. */
  spin?: boolean;
  className?: string;
}) {
  const small = compact ?? size < COMPACT_BELOW;
  const g = small ? MARK_COMPACT : MARK_FULL;
  const period = small ? MOTION.compactPeriod : MOTION.period;
  const count = small ? MOTION.compactSignals : MOTION.signals;
  const name = small ? "synapse-signal-compact" : "synapse-signal";

  const body = (
    <>
      {/* The tracks sit back so the signals read as the moving part. The static
          mark keeps full strength — a logo has to survive single-ink print,
          where a dimmed track simply disappears. */}
      <g opacity={0.4}>
        <Rings g={g} />
      </g>
      {Array.from({ length: count }, (_, i) => (
        <circle
          key={i}
          className="synapse-signal"
          cx={BRAND_GRID / 2}
          cy={BRAND_GRID / 2}
          r={g.beads.r}
          style={{
            animationName: name,
            animationDuration: `${period}s`,
            // Negative delays start each signal partway along, which is what
            // spreads them evenly around the route from the first frame rather
            // than letting them file out one at a time on load.
            animationDelay: `-${((period * i) / count).toFixed(3)}s`,
          }}
        />
      ))}
      <g className="synapse-parked">
        {g.beads.at.map(([x, y]) => (
          <circle key={`${x},${y}`} cx={x} cy={y} r={g.beads.r} />
        ))}
      </g>
    </>
  );

  return (
    <svg
      width={size}
      height={size}
      viewBox={`0 0 ${BRAND_GRID} ${BRAND_GRID}`}
      fill="currentColor"
      role={label ? "status" : undefined}
      aria-label={label}
      aria-hidden={label ? undefined : true}
      focusable="false"
      className={cn("shrink-0", className)}
    >
      {spin ? <g className="synapse-spin">{body}</g> : body}
    </svg>
  );
}
