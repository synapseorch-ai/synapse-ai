"use client";
/*
 * The application shell: persistent rail, one <main>, a real page.
 *
 * Replaces app/settings/layout.tsx, which rendered `fixed inset-0 z-50
 * bg-black/90 backdrop-blur-md` with a close X — a dialog drawn over a page
 * that was never there, because Settings is a route.
 *
 * Owns the theme for the whole app. Both the chat page and the old settings
 * shell used to read `synapseTheme` separately; now it is read once, and always
 * AFTER hydration — reading localStorage during render mismatches the SSR HTML.
 */
import { useEffect } from 'react';

import { TemplateSourcesProvider, TooltipProvider, usePersisted } from '@/components/ui';
import { BuilderLauncher } from '@/components/app/BuilderLauncher';
import { BuilderPanel } from '@/components/app/BuilderPanel';
import { BuilderProvider } from '@/components/app/BuilderProvider';
import { AppRail, type ThemeMode } from '@/components/app/AppRail';
import { NavGuardProvider } from '@/components/app/NavGuard';

/**
 * Variables for `{{ global.… }}` suggestions in every editor, fetched on the
 * first `{{`. Self-hosted variables are plain values; only the names leave here.
 */
async function loadVariables() {
    const response = await fetch('/api/settings/variables');
    if (!response.ok) return [];
    const { values } = (await response.json()) as { values?: Record<string, unknown> };
    return Object.keys(values ?? {}).map((key) => ({ key }));
}

export default function AppLayout({ children }: { children: React.ReactNode }) {
    // Three modes, one class. `system` follows the OS and keeps following it
    // while the tab is open; the two explicit modes pin the class regardless.
    const [mode, setMode] = usePersisted<ThemeMode>(
        'synapseTheme',
        raw => (raw === 'light' || raw === 'system' ? raw : 'dark'),
        'dark',
    );

    // The class goes on <html>, not on the shell div. `body` resolves
    // `--background` against itself, so a .light-mode starting below body left
    // the page ground dark behind a light app — visible on overscroll and
    // behind the app-wide toast host, which renders outside this shell.
    useEffect(() => {
        const root = document.documentElement;
        const media = window.matchMedia('(prefers-color-scheme: light)');
        const apply = () => {
            const light = mode === 'light' || (mode === 'system' && media.matches);
            root.classList.toggle('light-mode', light);
        };
        apply();
        if (mode !== 'system') return () => root.classList.remove('light-mode');
        media.addEventListener('change', apply);
        return () => {
            media.removeEventListener('change', apply);
            root.classList.remove('light-mode');
        };
    }, [mode]);

    // Swapping the palette drags every `transition-colors` in the app along at
    // once — a smear where controls are mid-way between the two while cards,
    // which have no transition, have already switched. Suppress transitions for
    // a frame around the swap.
    const changeMode = (next: ThemeMode) => {
        const root = document.documentElement;
        root.setAttribute('data-theme-switching', '');
        setMode(next);
        window.requestAnimationFrame(() => {
            window.requestAnimationFrame(() => root.removeAttribute('data-theme-switching'));
        });
    };

    return (
        <NavGuardProvider>
            {/* One provider for the app. Nested providers each keep their own
                open-delay timer, so tooltips start behaving differently
                depending on which subtree they happen to be in. */}
            <TooltipProvider>
            <TemplateSourcesProvider loadGlobals={loadVariables}>
            {/* The AI builder is mounted once, here, rather than by a screen:
                it is reachable from every screen and its conversation has to
                survive navigating between them. */}
            <BuilderProvider>
            <div className="flex h-screen overflow-hidden bg-bg font-sans text-text">
                <AppRail mode={mode} onModeChange={changeMode} />
                {/* The only <main> in the app. Pages render a plain flex column
                    inside it — a nested <main> gives two landmarks and two
                    scrollbars. */}
                <main className="min-w-0 flex-1 overflow-hidden">{children}</main>
            </div>
            <BuilderLauncher />
            <BuilderPanel />
            </BuilderProvider>
            </TemplateSourcesProvider>
            </TooltipProvider>
        </NavGuardProvider>
    );
}
