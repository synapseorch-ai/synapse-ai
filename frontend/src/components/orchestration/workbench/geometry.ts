/**
 * Where the open canvas's floating chrome sits, and so where the graph can be
 * seen (M3a). Pure, so fit, pan and placement all agree on one rectangle.
 */

/** The floating bar: 12px in, 42px tall. The graph starts below it. */
export const TOP_CLEAR = 66;
/** The dock and the zoom cluster: 12px in, about 46px tall. */
export const BOTTOM_CLEAR = 70;
/** Air around the graph inside the free rectangle. */
export const SIDE_CLEAR = 16;
/** The setup card's width, and its bounds (Prototype 4). */
export const CARD_DEFAULT = 340;
export const CARD_MIN = 300;
export const CARD_MAX = 560;
/** Below this the canvas is a phone's: the card is a bottom sheet. Tailwind's `md`. */
export const PHONE_QUERY = '(max-width: 767px)';

export interface Rect {
    left: number;
    top: number;
    right: number;
    bottom: number;
}

/**
 * The part of the canvas nothing floats over, in the canvas's own pixels.
 * `card` is the open card's width (0 when closed), `sheet` the open bottom
 * sheet's height on a phone, `insetRight` what a shell panel covers at the right.
 */
export function freeRect(opts: {
    width: number;
    height: number;
    card: number;
    sheet: number;
    insetRight: number;
}): Rect {
    const { width, height, card, sheet, insetRight } = opts;
    const right = width - insetRight - (card ? 12 + card + SIDE_CLEAR : SIDE_CLEAR);
    const bottom = sheet ? height - sheet - 12 : height - BOTTOM_CLEAR;
    return {
        left: SIDE_CLEAR,
        top: TOP_CLEAR + 10,
        right: Math.max(SIDE_CLEAR + 80, right),
        bottom: Math.max(TOP_CLEAR + 90, bottom),
    };
}

/** fitView's padding for a free rectangle, in pixels on each side. */
export function fitPadding(rect: Rect, width: number, height: number) {
    return {
        top: `${Math.round(rect.top)}px`,
        left: `${Math.round(rect.left)}px`,
        right: `${Math.round(Math.max(0, width - rect.right))}px`,
        bottom: `${Math.round(Math.max(0, height - rect.bottom))}px`,
    } as const;
}

/**
 * How far to move the view so a box (a step, on screen) sits inside the free
 * rectangle with a margin. Zero on an axis where it already does; a box larger
 * than the rectangle keeps its top-left corner in view.
 */
export function panToFit(box: Rect, free: Rect, margin = 16): { dx: number; dy: number } {
    const axis = (lo: number, hi: number, min: number, max: number) => {
        if (lo < min + margin) return min + margin - lo;
        if (hi > max - margin) return Math.max(min + margin - lo, max - margin - hi);
        return 0;
    };
    return {
        dx: axis(box.left, box.right, free.left, free.right),
        dy: axis(box.top, box.bottom, free.top, free.bottom),
    };
}

/**
 * How much room the controls have, from the width left of the card: the dock
 * drops its words below 940px and the zoom cluster keeps only Fit below 660px.
 */
export type Tier = 'full' | 'compact' | 'tight';
export function tierOf(freeWidth: number): Tier {
    if (freeWidth < 660) return 'tight';
    if (freeWidth < 940) return 'compact';
    return 'full';
}
