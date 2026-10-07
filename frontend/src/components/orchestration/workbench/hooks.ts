'use client';
import { useEffect, useState, useSyncExternalStore } from 'react';

/** A media query, live. The server render says no. */
export function useMediaQuery(query: string): boolean {
    return useSyncExternalStore(
        (onChange) => {
            const media = window.matchMedia(query);
            media.addEventListener('change', onChange);
            return () => media.removeEventListener('change', onChange);
        },
        () => window.matchMedia(query).matches,
        () => false,
    );
}

export const useReducedMotion = () => useMediaQuery('(prefers-reduced-motion: reduce)');

export type Box = { width: number; height: number; right: number };

/** An element's size, and how far its right edge is from the window's, kept current. */
export function useElementBox(ref: React.RefObject<HTMLElement | null>): Box {
    const [box, setBox] = useState<Box>({ width: 0, height: 0, right: 0 });
    useEffect(() => {
        const el = ref.current;
        if (!el) return;
        const read = () => {
            const r = el.getBoundingClientRect();
            setBox((prev) => {
                const next = { width: Math.round(r.width), height: Math.round(r.height), right: Math.round(window.innerWidth - r.right) };
                return prev.width === next.width && prev.height === next.height && prev.right === next.right ? prev : next;
            });
        };
        read();
        const observer = new ResizeObserver(read);
        observer.observe(el);
        window.addEventListener('resize', read);
        return () => {
            observer.disconnect();
            window.removeEventListener('resize', read);
        };
    }, [ref]);
    return box;
}
