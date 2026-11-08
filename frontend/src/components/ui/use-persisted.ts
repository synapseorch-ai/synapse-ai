"use client";
/*
 * localStorage as an external store.
 *
 * In the kit rather than next to the rail that first needed it: both products
 * persist a collapsed sidebar, and this is thirty lines with no product
 * knowledge in it at all.
 *
 * Reading it in an effect and calling setState causes a cascading render (and
 * React's lint rule says so); reading it during render mismatches the SSR HTML.
 * useSyncExternalStore does neither — the server snapshot is the fallback,
 * which is exactly what the server rendered. Cross-tab sync comes free.
 */
import { useCallback, useSyncExternalStore } from 'react';

const EVENT = 'synapse-local-storage';

function subscribe(onChange: () => void) {
    window.addEventListener(EVENT, onChange);
    window.addEventListener('storage', onChange);
    return () => {
        window.removeEventListener(EVENT, onChange);
        window.removeEventListener('storage', onChange);
    };
}

export function usePersisted<T extends string>(
    key: string,
    /** Must map any stored value — including null — onto a valid one. */
    parse: (raw: string | null) => T,
    /** Server snapshot. Whatever the SSR HTML assumes. */
    fallback: T,
): readonly [T, (next: T) => void] {
    const value = useSyncExternalStore(
        subscribe,
        () => parse(localStorage.getItem(key)),
        () => fallback,
    );

    const set = useCallback((next: T) => {
        localStorage.setItem(key, next);
        window.dispatchEvent(new Event(EVENT));
    }, [key]);

    return [value, set] as const;
}
