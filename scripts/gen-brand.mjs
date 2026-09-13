#!/usr/bin/env node
/**
 * Generate the SynapseOrch brand mark: path data, motion keyframes, rasters.
 *
 * The mark is three nested layers with a solid core, and the loader is a signal
 * running a strict route through them. Both come from the
 * numbers at the top of this file, which is the point: the animation is derived
 * from the mark's own radii and gap angles, so the two cannot drift apart. Edit
 * a radius here, re-run, and the logo, the favicon and the loader all move
 * together.
 *
 * THE ONE RULE THE GEOMETRY ENFORCES
 * Each layer carries its two gaps at the route's entry *and* exit angle, which
 * means consecutive layers share a gap. A signal therefore leaves one ring and
 * enters the next through a hole in *both*. An earlier version put the exit at
 * the next layer's gap instead, and signals visibly cut through solid arc.
 *
 *   node scripts/gen-brand.mjs            # write every artifact
 *   node scripts/gen-brand.mjs --print    # just print the path data
 */

import { writeFileSync, mkdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = join(HERE, "..");

/* ── The numbers ────────────────────────────────────────────────────────── */

const GRID = 24;                 // lucide's grid, so the mark aligns with icons
const C = GRID / 2;              // centre
const BASE = -90;                // the route starts at twelve o'clock
const LAYERS = 3;
const OUTER = 9.3, INNER = 3.5;  // outermost and innermost layer radii
const STROKE = 1.4;
const CORE = 1.3;                // the core disc every signal is absorbed into
const GAP_ARC = 6.4;             // target gap length, before clamping
const ACCENT = "#6bb4bd";

// The loader. Uniform period and evenly spaced releases: one signal enters
// every PERIOD/SIGNALS seconds, so the flow reads as a steady conveyor.
const PERIOD = 4.2, SIGNALS = 6;
// The compact form is the same mark with one layer removed, not a different
// shape. Its route is shorter, so it gets its own period chosen to hold the
// signal at the same units-per-second as the full mark.
const COMPACT_PERIOD = 3.6, COMPACT_SIGNALS = 3;
const COMPACT = { radii: [9.1, 5.0], stroke: 1.8, core: 1.7, step: 140 };
const SPIN = 48;                 // seconds per revolution of the whole assembly

/* ── Primitives ─────────────────────────────────────────────────────────── */

const r3 = (n) => Math.round(n * 1000) / 1000;
const rad = (d) => (d * Math.PI) / 180;
const pt = (r, deg) => [C + r * Math.cos(rad(deg)), C + r * Math.sin(rad(deg))];

function arc(r, a0, a1) {
  const [x0, y0] = pt(r, a0), [x1, y1] = pt(r, a1);
  const large = Math.abs(a1 - a0) > 180 ? 1 : 0;
  const sweep = a1 > a0 ? 1 : 0;
  return `M${r3(x0)} ${r3(y0)}A${r} ${r} 0 ${large} ${sweep} ${r3(x1)} ${r3(y1)}`;
}

/* ── Geometry ───────────────────────────────────────────────────────────── */

/**
 * Both forms are the same construction: nested layers, each carrying its two
 * gaps at the route's entry and exit angle, a bead parked on each entry, one
 * core. The compact form differs only in having two layers instead of three,
 * with the stroke and spacing opened up enough to survive 16px — measured by
 * rendering it, not assumed.
 *
 * Its step is 140 degrees rather than 180. At 180 the two layers' gaps would
 * land on the same axis and the spiral would flatten into a vertical corridor;
 * 140 keeps the gaps walking round the mark the way the full one does.
 */
function geometry(compact = false) {
  const step = compact ? COMPACT.step : 360 / LAYERS;
  const radii = compact
    ? COMPACT.radii
    : Array.from({ length: LAYERS }, (_, i) =>
        r3(OUTER - i * ((OUTER - INNER) / (LAYERS - 1))));
  if (compact) {
    const cap = Math.min(30, Math.min(step, 360 - step) / 2 - 6);
    const half = radii.map((r) =>
      r3(Math.min(cap, Math.max(15, ((GAP_ARC / 2 / r) * 180) / Math.PI))));
    return { radii, w: COMPACT.stroke, core: COMPACT.core, half, step, compact: true };
  }
  // A constant gap *length* runs away on the innermost ring — at r=3.5 it asks
  // for 52 degrees a side. It also may never reach half a layer's own gap
  // separation, or its two gaps merge and the ring falls apart.
  const cap = Math.min(30, Math.min(step, 360 - step) / 2 - 6);
  const half = radii.map((r) =>
    r3(Math.min(cap, Math.max(15, ((GAP_ARC / 2 / r) * 180) / Math.PI))));
  return { radii, w: STROKE, core: CORE, half, step, compact: false };
}

const entry = (g, i) => BASE + i * g.step;        // where a signal enters layer i
const exit_ = (g, i) => BASE + (i + 1) * g.step;  // and leaves — layer i+1's entry

function rings(g) {
  const out = [];
  g.radii.forEach((r, i) => {
    const h = g.half[i];
    out.push(arc(r, entry(g, i) + h, exit_(g, i) - h));       // the route's arc
    out.push(arc(r, exit_(g, i) + h, entry(g, i) + 360 - h)); // the long way round
  });
  return out;
}

/** A solid disc: the terminus every signal is absorbed into. */
function core(g) {
  const r = g.core;
  return [`M${C} ${r3(C - r)}a${r} ${r} 0 1 0 0 ${r3(r * 2)}a${r} ${r} 0 1 0 0 ${r3(-r * 2)}`];
}

/** Signals parked on the waypoints — the route drawn into the static mark. */
function beads(g) {
  const r = r3(g.w * 0.7 + 0.25);
  return { r, at: g.radii.map((rr, i) => pt(rr, entry(g, i)).map(r3)) };
}

/* ── Route ──────────────────────────────────────────────────────────────── */

function route(g) {
  const segs = [];
  g.radii.forEach((r, i) => {
    segs.push({ type: "arc", r, a0: entry(g, i), a1: exit_(g, i) });
    segs.push({ type: "rad", a: exit_(g, i), r0: r, r1: i + 1 < g.radii.length ? g.radii[i + 1] : 0 });
  });
  return segs;
}

const flatten = (s) => {
  if (s.type === "rad") return [pt(s.r0, s.a), s.r1 === 0 ? [C, C] : pt(s.r1, s.a)];
  const n = 96, out = [];
  for (let k = 0; k <= n; k++) out.push(pt(s.r, s.a0 + ((s.a1 - s.a0) * k) / n));
  return out;
};
const len = (p) => p.reduce((t, q, i) => (i ? t + Math.hypot(q[0] - p[i - 1][0], q[1] - p[i - 1][1]) : 0), 0);

function walk(poly, frac) {
  let want = len(poly) * frac, i = 1;
  while (i < poly.length - 1) {
    const d = Math.hypot(poly[i][0] - poly[i - 1][0], poly[i][1] - poly[i - 1][1]);
    if (want <= d) break;
    want -= d; i++;
  }
  const d = Math.hypot(poly[i][0] - poly[i - 1][0], poly[i][1] - poly[i - 1][1]) || 1;
  const f = want / d;
  return [poly[i - 1][0] + (poly[i][0] - poly[i - 1][0]) * f,
          poly[i - 1][1] + (poly[i][1] - poly[i - 1][1]) * f];
}

/**
 * Stops are allocated per segment so one always lands exactly on the corner
 * where an arc meets a radial step. Sampling evenly across the whole route
 * instead let the interpolation cut those corners by 0.24 units — visible as
 * the signal clipping the turn. Each stop carries its true arc-length
 * fraction, so the percentages are uneven and the speed stays constant.
 */
function sample(segs, n) {
  const polys = segs.map(flatten);
  const lens = polys.map(len);
  const total = lens.reduce((a, b) => a + b, 0);
  const out = [];
  let acc = 0;
  polys.forEach((poly, i) => {
    const k = Math.max(1, Math.round((lens[i] / total) * n));
    for (let j = 0; j < k; j++) {
      const [x, y] = walk(poly, j / k);
      out.push({ x, y, t: (acc + (lens[i] * j) / k) / total });
    }
    acc += lens[i];
  });
  const last = polys[polys.length - 1];
  out.push({ x: last[last.length - 1][0], y: last[last.length - 1][1], t: 1 });
  return { points: out, total };
}

function keyframes(name, pts) {
  let css = `@keyframes ${name} {\n`;
  for (const p of pts) {
    let o = 1, sc = 1;
    // 7% in is still inside the gap the signal spawned in; 93% is exactly where
    // the last arc ends and the drop into the core begins — so it arrives at
    // full strength and dies being absorbed, not in mid-flight.
    if (p.t < 0.07) { o = p.t / 0.07; sc = 0.45 + 0.55 * (p.t / 0.07); }
    else if (p.t > 0.93) { const u = (p.t - 0.93) / 0.07; o = 1 - u; sc = 1 - 0.7 * u; }
    css += `  ${r3(p.t * 100)}% { transform: translate(${r3(p.x - C)}px, ${r3(p.y - C)}px) scale(${r3(sc)}); opacity: ${r3(o)}; }\n`;
  }
  return css + "}\n";
}

/* ── Emit ───────────────────────────────────────────────────────────────── */

const full = geometry(false), compact = geometry(true);

function markSvg(g, { colour = "currentColor", withBeads = true } = {}) {
  const b = beads(g);
  return [
    ...rings(g).map((d) =>
      `<path d="${d}" fill="none" stroke="${colour}" stroke-width="${g.w}" stroke-linecap="butt"/>`),
    ...core(g).map((d) => `<path d="${d}" fill="${colour}"/>`),
    ...(withBeads ? b.at.map(([x, y]) => `<circle cx="${x}" cy="${y}" r="${b.r}" fill="${colour}"/>`) : []),
  ].join("");
}

const wrap = (body, colour) =>
  `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${GRID} ${GRID}" fill="none">${body}</svg>`;

const artifacts = {};
artifacts.markFull = markSvg(full);
artifacts.markCompact = markSvg(compact);
artifacts.motion =
  keyframes("synapse-signal", sample(route(full), 56).points) +
  keyframes("synapse-signal-compact", sample(route(compact), 56).points);

/**
 * The invariant, checked rather than trusted: a signal may ride its own layer's
 * arc, but on a radial step every band it passes through must be open at that
 * angle. This is the rule the whole geometry exists to satisfy, so it ships
 * with the generator — `--audit` fails loudly if a future edit breaks it.
 */
function audit(g) {
  const diff = (a, b) => ((((a - b) % 360) + 540) % 360) - 180;
  const open = (i, a) => [entry(g, i), exit_(g, i)].some((w) => Math.abs(diff(a, w)) <= g.half[i]);
  let closed = 0, stray = 0, worst = 0;
  for (const seg of route(g)) {
    const poly = flatten(seg);
    for (let k = 0; k <= 600; k++) {
      const [x, y] = walk(poly, k / 600);
      const a = (Math.atan2(y - C, x - C) * 180) / Math.PI;
      const pr = Math.hypot(x - C, y - C);
      g.radii.forEach((r, i) => {
        const depth = g.w / 2 - Math.abs(pr - r);
        if (depth <= 1e-6) return;
        if (seg.type === "arc") { if (seg.r !== r) { stray++; worst = Math.max(worst, depth); } }
        else if (!open(i, a)) { closed++; worst = Math.max(worst, depth); }
      });
    }
  }
  const reach = Math.max(
    g.radii[0] + g.w / 2,
    ...beads(g).at.map(([x, y]) => Math.hypot(x - C, y - C) + beads(g).r));
  return { closed, stray, worst, reach };
}

if (process.argv.includes("--audit")) {
  let bad = 0;
  for (const [name, g] of [["full", full], ["compact", compact]]) {
    const a = audit(g);
    const fits = a.reach <= GRID / 2 - 1 + 1e-9;
    const ok = a.closed === 0 && a.stray === 0 && fits;
    console.log(`${name.padEnd(8)} radial step through a closed band: ${a.closed}`
      + `  strayed into another layer: ${a.stray}`
      + `  reach ${r3(a.reach)}u ${fits ? "fits" : "CLIPS"}`
      + `  ${ok ? "PASS" : "FAIL"}`);
    if (!ok) bad++;
    // the two gaps on a layer must not merge
    g.radii.forEach((r, i) => {
      const sep = Math.min(g.step, 360 - g.step);
      if (!g.compact && 2 * g.half[i] >= sep) { console.log(`  layer ${i + 1} gaps merge`); bad++; }
    });
  }
  const stops = sample(route(full), 56).points;
  const mono = stops.every((p, i, a) => i === 0 || p.t >= a[i - 1].t);
  console.log(`keyframes ${stops.length} stops, monotonic: ${mono ? "yes" : "NO"}`);
  if (!mono) bad++;
  console.log(bad ? `\n${bad} FAILURE(S)` : "\nAUDIT PASSED");
  process.exit(bad ? 1 : 0);
}

if (process.argv.includes("--print")) {
  console.log("── full ──\n" + artifacts.markFull);
  console.log("\n── compact ──\n" + artifacts.markCompact);
  console.log("\n── geometry ──");
  console.log("radii      ", full.radii.join(", "));
  console.log("half-angles", full.half.join(", "));
  console.log("waypoints  ", full.radii.map((_, i) => r3(((entry(full, i) % 360) + 360) % 360)).join(", "));
  console.log("route      ", r3(sample(route(full), 56).total), "units");
  console.log("bead radius", beads(full).r);
  console.log("timing     ", `${PERIOD}s / ${SIGNALS} signals = one every ${r3(PERIOD / SIGNALS)}s`);
  process.exit(0);
}

/* ── Generated modules ──────────────────────────────────────────────────────
   `brand.tsx` reads its geometry from here rather than carrying hand-copied
   coordinates, and the keyframes land in their own stylesheet that both apps
   import. Everything downstream of this file is generated, so the mark, the
   favicon and the loader cannot fall out of step with each other.
   ------------------------------------------------------------------------- */
const paths = `/**
 * GENERATED by scripts/gen-brand.mjs — do not edit.
 *
 * Re-run \`node scripts/gen-brand.mjs\` after changing any number in that file.
 * \`node scripts/gen-brand.mjs --audit\` checks the invariant this geometry
 * exists to satisfy: a signal only ever crosses between layers through a gap
 * that belongs to both of them.
 */

/** The mark is drawn on lucide's 24 grid, so it aligns with the icon set. */
export const BRAND_GRID = ${GRID};

export interface MarkGeometry {
  /** Arc segments, stroked. */
  readonly rings: readonly string[];
  /** The core disc, as a path so it renders through the same code as the rings. */
  readonly core: readonly string[];
  /** Signals parked on the route's waypoints. */
  readonly beads: { readonly r: number; readonly at: readonly (readonly [number, number])[] };
  readonly stroke: number;
}

/** Three layers and a solid core. Needs ~20px to read. */
export const MARK_FULL: MarkGeometry = {
  rings: [
${rings(full).map((d) => `    ${JSON.stringify(d)},`).join("\n")}
  ],
  core: [
${core(full).map((d) => `    ${JSON.stringify(d)},`).join("\n")}
  ],
  beads: { r: ${beads(full).r}, at: [${beads(full).at.map(([x, y]) => `[${x}, ${y}]`).join(", ")}] },
  stroke: ${full.w},
};

/**
 * The same mark with one layer removed. Below about 20px the full mark's three
 * layers merge into a disc; two layers at this weight still read. Used by the
 * favicon and the inline chat loader.
 */
export const MARK_COMPACT: MarkGeometry = {
  rings: [
${rings(compact).map((d) => `    ${JSON.stringify(d)},`).join("\n")}
  ],
  core: [
${core(compact).map((d) => `    ${JSON.stringify(d)},`).join("\n")}
  ],
  beads: { r: ${beads(compact).r}, at: [${beads(compact).at.map(([x, y]) => `[${x}, ${y}]`).join(", ")}] },
  stroke: ${compact.w},
};

/** The size below which \`Mark\` and \`BrandLoader\` switch to the compact form. */
export const COMPACT_BELOW = 20;

/**
 * Loader timing. One period for every signal, releases spread evenly across it,
 * so a signal enters every \`period / signals\` seconds and the flow reads as a
 * steady conveyor rather than a crowd.
 */
export const MOTION = {
  period: ${PERIOD},
  signals: ${SIGNALS},
  compactPeriod: ${COMPACT_PERIOD},
  compactSignals: ${COMPACT_SIGNALS},
  /** Seconds per revolution of the whole assembly. */
  spin: ${SPIN},
} as const;
`;
writeFileSync(join(ROOT, "frontend/src/components/ui/brand.paths.ts"), paths);
console.log("wrote frontend/src/components/ui/brand.paths.ts");

const css = `/**
 * GENERATED by scripts/gen-brand.mjs — do not edit.
 *
 * The brand mark's motion. Imported by \`globals.css\` in both the open-source
 * app and the cloud app; \`brand.tsx\` renders markup that depends on these
 * names existing, which is the same cross-repo contract \`--ui-radius\` has.
 *
 * A signal enters the outer layer through its gap, runs the arc, leaves through
 * its other gap, steps inward, and is absorbed by the core. Stops are placed per
 * segment so one lands exactly on every corner where an arc meets a radial step;
 * their percentages are uneven so the speed stays constant.
 */

/* transform-box and transform-origin are load-bearing: an SVG element's
   transform origin is the user-space origin by default, so scale() would sling
   the signal away from its own centre instead of resizing it in place. */
.synapse-signal {
  animation-timing-function: linear;
  animation-iteration-count: infinite;
  opacity: 0;
  transform-box: view-box;
  transform-origin: 12px 12px;
}

/* The loader's rings. Dimmed so the travelling signals read as the moving
   part — but a dim accent on white is a much weaker line than on near-black,
   so light theme sets them back less (both products' light switches). */
.synapse-track { opacity: 0.4; }
:root[data-theme="light"] .synapse-track,
.light-mode .synapse-track { opacity: 0.7; }

/* The assembly turns as one body. Turning the layers at different rates would
   slide their shared gaps out of register, and a shared gap is the only reason
   a signal can cross between two layers at all. */
.synapse-spin {
  animation: synapse-spin ${SPIN}s linear infinite;
  transform-box: view-box;
  transform-origin: 12px 12px;
}

@keyframes synapse-spin {
  to { transform: rotate(360deg); }
}

/* Reduced motion swaps the travelling signals for parked ones, so the mark
   still says "work in progress" without anything moving. */
.synapse-parked { display: none; }

@media (prefers-reduced-motion: reduce) {
  .synapse-signal { display: none; }
  .synapse-parked { display: inline; }
  .synapse-spin { animation: none; }
}

${artifacts.motion}`;
writeFileSync(join(ROOT, "frontend/src/app/brand-motion.css"), css);
console.log("wrote frontend/src/app/brand-motion.css");

/* Static SVG exports. These carry a real colour because they are used where
   `currentColor` has nothing to inherit from — a README, an OG card, an
   uploaded avatar. The React component keeps `currentColor`. */
const outSvg = join(ROOT, "frontend/public/logo.svg");
mkdirSync(dirname(outSvg), { recursive: true });
writeFileSync(outSvg, wrap(markSvg(full, { colour: ACCENT }), ACCENT) + "\n");
writeFileSync(join(ROOT, "frontend/public/logo-mono.svg"),
  wrap(markSvg(full, { colour: "#ffffff" }), "#ffffff") + "\n");
writeFileSync(join(ROOT, "frontend/public/logo-compact.svg"),
  wrap(markSvg(compact, { colour: ACCENT }), ACCENT) + "\n");
console.log("wrote frontend/public/logo.svg, logo-mono.svg, logo-compact.svg");

/* Rasters. A favicon cannot inherit a colour and cannot know the browser's
   theme, so it ships as the accent mark on a transparent ground — legible on
   both the light and dark tab strips, unlike a mark that assumes one of them.
   
   THE FAVICON USES THE COMPACT GEOMETRY, NOT THE FULL MARK. A browser scales
   one file down to 16px, and at that size the full mark's three layers merge
   into a disc — rendered and looked at, not assumed. The compact form is the
   same construction with one layer dropped, so the tab and the app still show
   recognisably the same mark. */
const sharp = (await import(join(ROOT, "frontend/node_modules/sharp/lib/index.js"))).default;
const png = async (size, path, { pad = 0, g = full } = {}) => {
  const inner = size - pad * 2;
  const svg = Buffer.from(wrap(markSvg(g, { colour: ACCENT }), ACCENT));
  const img = await sharp(svg, { density: 1200 }).resize(inner, inner).png().toBuffer();
  const out = pad
    ? await sharp({ create: { width: size, height: size, channels: 4, background: { r: 9, g: 9, b: 11, alpha: 1 } } })
        .composite([{ input: img, top: pad, left: pad }]).png().toBuffer()
    : img;
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, out);
  console.log(`wrote ${path.replace(ROOT + "/", "")} (${size}px, ${out.length} B)`);
};

await png(512, join(ROOT, "frontend/src/app/icon.png"), { g: compact });
// Apple pads and squares the icon itself and never honours transparency, so
// this one gets the dark ground baked in rather than a black rectangle later.
// 180px is ample room, so it carries the full mark.
await png(180, join(ROOT, "frontend/src/app/apple-icon.png"), { pad: 26, g: full });
