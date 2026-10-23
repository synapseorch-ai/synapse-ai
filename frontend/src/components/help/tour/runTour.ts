'use client';

/*
 * Draws a tour with driver.js (MIT; GUIDE1's call, Naveen OK'd 2026-10-23): it already does the cut-out over the
 * page, scrolling a step into view, keys (→ ← Esc) and following resizes. Its look is ours: the popover takes the
 * kit's tokens through `POPOVER` below, so it follows the theme, light or dark, and driver's own sheet only lays
 * out the overlay. Loaded when a tour starts, so pages without one never fetch it.
 */
import 'driver.js/dist/driver.css';

import type { TourStep } from '../host';

import { anchorOf, shownSteps } from './tour';

/** The popover in the kit's terms; driver's classes are reached from its root. */
const POPOVER = [
    '!max-w-sm !rounded-lg !border !border-border-strong !bg-surface !p-4 !text-text !shadow-elevated',
    '[&_.driver-popover-title]:!text-sm [&_.driver-popover-title]:!font-semibold [&_.driver-popover-title]:!text-text',
    '[&_.driver-popover-description]:!mt-1 [&_.driver-popover-description]:!text-sm [&_.driver-popover-description]:!leading-relaxed [&_.driver-popover-description]:!text-text-muted',
    '[&_.driver-popover-progress-text]:!text-xs [&_.driver-popover-progress-text]:!text-text-faint',
    '[&_.driver-popover-footer_button]:!rounded-md [&_.driver-popover-footer_button]:!border-border [&_.driver-popover-footer_button]:!bg-surface-2 [&_.driver-popover-footer_button]:!px-3 [&_.driver-popover-footer_button]:!py-1 [&_.driver-popover-footer_button]:!text-xs [&_.driver-popover-footer_button]:!text-text [&_.driver-popover-footer_button]:![text-shadow:none]',
    '[&_.driver-popover-next-btn]:!border-accent [&_.driver-popover-next-btn]:!bg-accent [&_.driver-popover-next-btn]:!text-accent-fg',
    '[&_.driver-popover-close-btn]:!text-text-faint',
    '[&_.driver-popover-arrow]:!hidden',
].join(' ');

/**
 * Runs the steps whose anchors are on the page now. `onEnd` runs once, however the tour ends (Done, ✕, Esc, a
 * click outside), and at once when no step can show.
 */
export async function runTour(steps: readonly TourStep[], onEnd: () => void): Promise<void> {
    const shown = shownSteps(steps, anchor => anchorOf(anchor) !== null);
    if (shown.length === 0) {
        onEnd();
        return;
    }
    const { driver } = await import('driver.js');
    const tour = driver({
        showProgress: shown.length > 1,
        progressText: '{{current}} of {{total}}',
        nextBtnText: 'Next',
        prevBtnText: 'Back',
        doneBtnText: 'Done',
        popoverClass: POPOVER,
        overlayColor: 'black',
        overlayOpacity: 0.5,
        stagePadding: 6,
        stageRadius: 8,
        // Each anchor is read again as its step comes up: the page may have moved since the tour began.
        steps: shown.map(step => ({
            element: () => anchorOf(step.anchor) ?? document.body,
            popover: { title: step.title, description: step.body },
        })),
        onDestroyed: onEnd,
    });
    tour.drive();
}
