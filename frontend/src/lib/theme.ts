"use client";
/*
 * The theme: one key, one attribute, one way to switch.
 *
 * The palette is keyed off `data-theme="light|dark"` on <html> (`app/tokens.css`),
 * as in the cloud product and the site, so the three can share one token file.
 * It used to be a `.light-mode` class here, which no shared file could follow.
 *
 * The choice is three-way and kept in localStorage under `synapseTheme`.
 * Nothing saved means `system` (M0a, 2026-10-07): a new install follows the OS
 * and keeps following it while the tab is open. Only an explicit Light or Dark
 * is stored. The first paint is the root layout's inline script, which applies
 * the same rule before any of this runs.
 */
import { useSyncExternalStore } from 'react';

export type ThemeMode = 'system' | 'light' | 'dark';
export type Theme = 'light' | 'dark';

export const THEME_KEY = 'synapseTheme';

/** Any stored value, including none, onto a valid mode. */
export function parseThemeMode(raw: string | null): ThemeMode {
    return raw === 'light' || raw === 'dark' ? raw : 'system';
}

/** What a mode paints right now. */
export function resolveTheme(mode: ThemeMode): Theme {
    if (mode !== 'system') return mode;
    return window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
}

/**
 * Change the mode without a smear. Almost every control carries
 * `transition-colors`, so swapping the palette would drag each of them between
 * the two themes while the cards, which have no transition, have already
 * switched. `data-theme-switching` turns transitions off for a frame around it.
 */
export function switchTheme(setMode: (mode: ThemeMode) => void, next: ThemeMode) {
    const root = document.documentElement;
    root.setAttribute('data-theme-switching', '');
    setMode(next);
    window.requestAnimationFrame(() => {
        window.requestAnimationFrame(() => root.removeAttribute('data-theme-switching'));
    });
}

function subscribe(onChange: () => void) {
    const observer = new MutationObserver(onChange);
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
    return () => observer.disconnect();
}

/** The theme on the page, as the shell applied it. Dark on the server, as the SSR HTML is. */
export function useAppliedTheme(): Theme {
    return useSyncExternalStore(
        subscribe,
        () => (document.documentElement.dataset.theme === 'light' ? 'light' : 'dark'),
        () => 'dark',
    );
}
