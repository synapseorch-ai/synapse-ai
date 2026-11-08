/**
 * One spelling for a dollar amount.
 *
 * There were three, and they disagreed about whether small money exists.
 * `Dashboard`'s was `toFixed(2)`, so a model that had cost $0.0003255 was
 * rendered `$0.00` on the screen headed "Where the workspace stands right
 * now" — with its own bar chart drawn beside it, so the row said both "there
 * is spend here" and "there is none". The Usage screen, reading the same API
 * in the same instant, showed `$0.004155`. A reader had no way to tell
 * "nothing was spent" from "less than half a cent was spent".
 *
 * Model calls are priced per million tokens, so a real figure being smaller
 * than a cent is the normal case here, not an edge one. Two decimal places is
 * the wrong unit for this product's money and always was.
 */

/** Below this, a figure is reported as a bound rather than a rounded zero. */
const FLOOR = 0.000001;

/**
 * A dollar amount, exactly: four decimal places, six below a cent, and a
 * bound below a millionth. `null` is "we have no figure", which is not zero.
 *
 * This is the default. Reach for `usdCompact` only where a large number has
 * to fit in a tile.
 */
export function usd(n: number | null | undefined): string {
    if (n === null || n === undefined || Number.isNaN(n)) return '—';
    if (n < 0) return `-${usd(-n)}`;
    // Exactly zero is the one case that may be written short: it is the only
    // figure `$0.00` describes truthfully.
    if (n === 0) return '$0.00';
    if (n < FLOOR) return `<$${FLOOR.toFixed(6)}`;
    if (n < 0.01) return `$${n.toFixed(6)}`;
    return `$${n.toFixed(4)}`;
}

/**
 * The same, but thousands are abbreviated so a headline stat stays one line.
 *
 * Only the large end differs, and only where the exact cents of a four-figure
 * sum are noise. The small end is `usd`'s, because that is where the lying was.
 */
export function usdCompact(n: number | null | undefined): string {
    if (typeof n === 'number' && n >= 1000) return `$${(n / 1000).toFixed(1)}k`;
    return usd(n);
}
