/**
 * The mark an app wears (an MCP server, a catalogue entry, Google) — in a card,
 * a list row or a feature card. Both products' Apps pages use it (M2f).
 *
 * Three sources, first match wins: a vendor's own SVG (cloud's catalogue
 * `icon_svg`, set through the staff console and sanitised on write), a static
 * image of ours (`/providers/google-icon.svg`), or a letter tile.
 *
 * The tile's hue is hashed from the name, so a grid of fifty reads as one
 * family with hue doing the distinguishing. Its lightness and chroma come from
 * the theme (`--tile-*` in tokens.css): a pale ground with a deep letter on
 * white, a deep ground with a light letter on dark. The old tile was a fixed
 * dark `hsl(… 45% 22%)` that stayed dark on white (UX1).
 */

import { cn } from "@/lib/cn";

/** A stable hue (0–359) for a name. */
export function tileHue(seed: string): number {
  let hash = 0;
  for (let i = 0; i < seed.length; i += 1) hash = (hash * 31 + seed.charCodeAt(i)) % 360;
  return hash;
}

const BOX = {
  // A list row's mark: a picker's option, beside its name (PICK1, F1482).
  xs: "size-5 rounded text-2xs [&>svg]:size-3.5",
  sm: "size-7 rounded-md text-xs [&>svg]:size-4",
  md: "size-9 rounded-lg text-sm [&>svg]:size-5",
  lg: "size-11 rounded-xl text-base [&>svg]:size-6",
} as const;

const IMG = { xs: "size-3.5", sm: "size-4", md: "size-5", lg: "size-6" } as const;

export function AppMark({
  name,
  seed,
  iconSvg,
  imgSrc,
  imgClassName,
  size = "md",
  className,
}: {
  /** What the letter comes from. */
  name: string;
  /** What the hue comes from; defaults to `name`. */
  seed?: string;
  /** A sanitised vendor SVG (cloud's catalogue). Safe because its write path
      re-serialises an allowlisted tree (`api/control/svg_icon.py`). */
  iconSvg?: string | null;
  /** A static asset of ours, e.g. `/providers/google-icon.svg`. */
  imgSrc?: string | null;
  /** A class for the image itself: a logo that must follow the theme (`theme-adaptive-icon`). */
  imgClassName?: string;
  size?: keyof typeof BOX;
  className?: string;
}) {
  const box = cn("flex shrink-0 items-center justify-center overflow-hidden", BOX[size], className);

  if (iconSvg) {
    return <span aria-hidden className={cn(box, "bg-surface-2")} dangerouslySetInnerHTML={{ __html: iconSvg }} />;
  }
  if (imgSrc) {
    return (
      <span aria-hidden className={cn(box, "bg-surface-2")}>
        {/* eslint-disable-next-line @next/next/no-img-element -- a static svg of ours */}
        <img src={imgSrc} alt="" className={cn(IMG[size], imgClassName)} />
      </span>
    );
  }
  const hue = tileHue((seed || name).toLowerCase());
  return (
    <span
      aria-hidden
      data-app-tile=""
      className={cn(box, "font-bold")}
      style={{
        backgroundColor: `oklch(var(--tile-bg-l) var(--tile-bg-c) ${hue})`,
        color: `oklch(var(--tile-fg-l) var(--tile-fg-c) ${hue})`,
      }}
    >
      {name.trim().slice(0, 1).toUpperCase() || "?"}
    </span>
  );
}
