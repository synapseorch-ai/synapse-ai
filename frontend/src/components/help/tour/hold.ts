'use client';

/*
 * What holds a first-visit tour back (ONB3, D326, F1060): a first-run surface that has the screen or is about to
 * (OSS's model step while its readiness is read, cloud's onboarding while the workspace has not been through it).
 * Each holds while it is up; the page's tour starts once the last lets go. Synced into cloud with `components/help`.
 */
import { useEffect, useSyncExternalStore } from 'react';

const holds = new Map<symbol, string>();
const listeners = new Set<() => void>();

function changed() {
    for (const listener of listeners) listener();
}

/** Hold tours back until the returned release is called; `reason` names the holder, for a reader of `heldBy`. */
export function holdTours(reason: string): () => void {
    const key = Symbol(reason);
    holds.set(key, reason);
    changed();
    return () => {
        if (holds.delete(key)) changed();
    };
}

/** Whether anything holds tours back now. */
export function toursHeld(): boolean {
    return holds.size > 0;
}

/** Who holds them, in the order they took hold. */
export function heldBy(): string[] {
    return [...holds.values()];
}

export function subscribeTourHolds(listener: () => void): () => void {
    listeners.add(listener);
    return () => { listeners.delete(listener); };
}

/** Hold tours back for as long as `active` is true and the caller is mounted. */
export function useTourHold(active: boolean, reason: string) {
    useEffect(() => (active ? holdTours(reason) : undefined), [active, reason]);
}

/** Whether tours are held, as a React value; during the server render they are (nothing starts before hydration). */
export function useToursHeld(): boolean {
    return useSyncExternalStore(subscribeTourHolds, toursHeld, () => true);
}

/** Whether a first-visit tour may start its countdown: nothing holds it and the help panel is shut. */
export function tourMayStart({ held, panelOpen }: { held: boolean; panelOpen: boolean }): boolean {
    return !held && !panelOpen;
}
