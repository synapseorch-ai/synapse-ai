/*
 * A guided tour's rules, apart from what draws it (GUIDE1, D318, F919): which steps can show, and whether this
 * browser has seen a page's tour. Synced into cloud's web with the rest of `components/help`.
 */
import type { Tour, TourStep } from '../host';

/** The element a step lights up, if it is on the page and laid out (a hidden one, as on a phone, is skipped). */
export function anchorOf(anchor: string, root: ParentNode = document): HTMLElement | null {
    const el = root.querySelector<HTMLElement>(`[data-tour="${CSS.escape(anchor)}"]`);
    return el && el.getClientRects().length > 0 ? el : null;
}

/** The steps that can show now, in order. */
export function shownSteps(steps: readonly TourStep[], present: (anchor: string) => boolean): TourStep[] {
    return steps.filter(step => present(step.anchor));
}

const seenKey = (id: string, tour: Tour) => `synapse.tour.${id}.v${tour.version}`;

/**
 * Whether this browser has seen (finished or put away) this version of a page's tour. Storage can be missing or
 * refuse (a private window): then the tour counts as seen, so it never shows on every visit.
 */
export function tourSeen(id: string, tour: Tour): boolean {
    try {
        return window.localStorage.getItem(seenKey(id, tour)) === '1';
    } catch {
        return true;
    }
}

export function markTourSeen(id: string, tour: Tour): void {
    try {
        window.localStorage.setItem(seenKey(id, tour), '1');
    } catch {
        // Nothing to remember it in; `tourSeen` already says seen.
    }
}

/** Something else has the screen: a dialog (onboarding, model setup) or a drawer. A tour waits for it to close. */
export function screenBusy(root: ParentNode = document): boolean {
    return root.querySelector('[role="dialog"], [role="alertdialog"]') !== null;
}
