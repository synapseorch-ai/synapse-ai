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
import { HelpProvider } from '@/components/help';
import { AppRail } from '@/components/app/AppRail';
import { NavGuardProvider } from '@/components/app/NavGuard';
import { NotificationProvider } from '@/components/notifications/NotificationProvider';
import { ossHelpHost } from '@/lib/helpHost';
import { parseThemeMode, resolveTheme, switchTheme, THEME_KEY, type ThemeMode } from '@/lib/theme';

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
    // Three modes, one attribute. `system` (also what nothing saved means)
    // follows the OS and keeps following it while the tab is open; the two
    // explicit modes pin the attribute regardless.
    const [mode, setMode] = usePersisted<ThemeMode>(THEME_KEY, parseThemeMode, 'system');

    // The attribute goes on <html>, not on the shell div. `body` resolves
    // `--background` against itself, so a theme starting below body left the
    // page ground dark behind a light app — visible on overscroll and behind
    // the app-wide toast host, which renders outside this shell. `color-scheme`
    // follows the attribute in `tokens.css`.
    //
    // The first paint is the root layout's script's (F220); this keeps the
    // attribute in step from then on, and clears it on leaving the shell
    // (/login is dark by design).
    useEffect(() => {
        const root = document.documentElement;
        const media = window.matchMedia('(prefers-color-scheme: light)');
        const apply = () => root.setAttribute('data-theme', resolveTheme(mode));
        const clear = () => root.removeAttribute('data-theme');
        apply();
        if (mode !== 'system') return clear;
        media.addEventListener('change', apply);
        return () => {
            media.removeEventListener('change', apply);
            clear();
        };
    }, [mode]);

    const changeMode = (next: ThemeMode) => switchTheme(setMode, next);

    return (
        <NavGuardProvider>
            {/* Run notifications are the signed-in app's, so the stream is
                opened here and not in the root layout, which /login and the
                widget's iframe share (F178). */}
            <NotificationProvider>
            {/* One provider for the app. Nested providers each keep their own
                open-delay timer, so tooltips start behaving differently
                depending on which subtree they happen to be in. */}
            <TooltipProvider>
            <TemplateSourcesProvider loadGlobals={loadVariables}>
            {/* The AI builder is mounted once, here, rather than by a screen:
                it is reachable from every screen and its conversation has to
                survive navigating between them. */}
            <BuilderProvider>
            {/* The "?" in every page header and the one panel it opens (H1):
                it fills the kit's header slot, so pages need nothing. */}
            <HelpProvider host={ossHelpHost}>
            <div className="flex h-screen overflow-hidden bg-bg font-sans text-text">
                <AppRail mode={mode} onModeChange={changeMode} />
                {/* The only <main> in the app. Pages render a plain flex column
                    inside it — a nested <main> gives two landmarks and two
                    scrollbars. */}
                <main className="min-w-0 flex-1 overflow-hidden">{children}</main>
            </div>
            </HelpProvider>
            <BuilderLauncher />
            <BuilderPanel />
            </BuilderProvider>
            </TemplateSourcesProvider>
            </TooltipProvider>
            </NotificationProvider>
        </NavGuardProvider>
    );
}
