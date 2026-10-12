"use client";
/*
 * The application shell: the rail on the frame colour, and one <main> — the
 * sheet — inset beside it with a hairline, a radius and a shadow (UX1, M1a).
 * On a phone the sheet fills the screen and a top bar opens the rail.
 *
 * Replaces app/settings/layout.tsx, which rendered `fixed inset-0 z-50
 * bg-black/90 backdrop-blur-md` with a close X — a dialog drawn over a page
 * that was never there, because Settings is a route.
 *
 * Owns the theme for the whole app. Both the chat page and the old settings
 * shell used to read `synapseTheme` separately; now it is read once, and always
 * AFTER hydration — reading localStorage during render mismatches the SSR HTML.
 */
import { useEffect, useState } from 'react';

import {
    CanvasModeProvider, CommandRegistryProvider, TemplateSourcesProvider, TooltipProvider, useCommandShortcut,
    useCommandShortcutLabel, usePersisted,
} from '@/components/ui';
import { AppCommands } from '@/components/app/AppCommands';
import { BuilderPanel } from '@/components/app/BuilderPanel';
import { BuilderProvider } from '@/components/app/BuilderProvider';
import { HelpProvider } from '@/components/help';
import { AppRail } from '@/components/app/AppRail';
import { NavFlagsProvider } from '@/components/app/NavFlags';
import { PhoneBar, useIsPhone } from '@/components/app/PhoneBar';
import { NavGuardProvider } from '@/components/app/NavGuard';
import { NotificationProvider } from '@/components/notifications/NotificationProvider';
import { WaitingOnYouHost } from '@/components/app/WaitingOnYou';
import { ossHelpHost } from '@/lib/helpHost';
import { parseThemeMode, resolveTheme, switchTheme, THEME_KEY, type ThemeMode } from '@/lib/theme';
import { cn } from '@/lib/utils';

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

/**
 * The browser's own chrome (a phone's status bar, a PWA's title bar) in the
 * frame's colour (M0a's note, M1a). The root layout declares both themes' frames
 * by media query for a first paint; once the shell knows the theme it writes the
 * one in force into both, so a light choice on a dark OS is light too. `null`
 * puts the declared values back, for the pages outside the shell.
 */
const THEME_COLOR_DEFAULTS = new WeakMap<HTMLMetaElement, string>();
function paintThemeColor(color?: string | null) {
    const frame = color === null
        ? null
        : color ?? getComputedStyle(document.documentElement).getPropertyValue('--frame').trim();
    document.querySelectorAll<HTMLMetaElement>('meta[name="theme-color"]').forEach((meta) => {
        if (!THEME_COLOR_DEFAULTS.has(meta)) THEME_COLOR_DEFAULTS.set(meta, meta.content);
        meta.content = frame || THEME_COLOR_DEFAULTS.get(meta) || meta.content;
    });
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
        const apply = () => {
            root.setAttribute('data-theme', resolveTheme(mode));
            paintThemeColor();
        };
        const clear = () => {
            root.removeAttribute('data-theme');
            paintThemeColor(null);
        };
        apply();
        if (mode !== 'system') return clear;
        media.addEventListener('change', apply);
        return () => {
            media.removeEventListener('change', apply);
            clear();
        };
    }, [mode]);
    const isPhone = useIsPhone();

    // ⌘K (Ctrl+K off a Mac) from anywhere, and the rail's Jump to… row.
    const [jumping, setJumping] = useState(false);
    useCommandShortcut(() => setJumping(open => !open));
    const jumpHint = useCommandShortcutLabel();
    const jump = () => setJumping(true);

    const changeMode = (next: ThemeMode) => switchTheme(setMode, next);

    return (
        <NavGuardProvider>
            {/* An open canvas folds the rail and takes the page header's place;
                a page's own ⌘K group joins the list (M3a). */}
            <CanvasModeProvider>
            <CommandRegistryProvider>
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
            <NavFlagsProvider>
            <div className="flex h-dvh overflow-hidden bg-frame font-sans text-text">
                <div className="hidden md:flex">
                    {/* On a phone the top bar holds the bell; one bell at a time. */}
                    <AppRail mode={mode} onModeChange={changeMode} showBell={!isPhone} onJump={jump} jumpHint={jumpHint} />
                </div>
                {/* The only <main> in the app, and the sheet the page sits on.
                    Pages render a plain flex column inside it — a nested <main>
                    gives two landmarks and two scrollbars. */}
                <main className={cn(
                    'flex min-w-0 flex-1 flex-col overflow-hidden bg-bg',
                    'md:my-2 md:mr-2 md:rounded-2xl md:border md:border-border md:shadow-elevated',
                )}>
                    <PhoneBar mode={mode} onModeChange={changeMode} onJump={jump} jumpHint={jumpHint} />
                    <div className="min-h-0 flex-1">{children}</div>
                </main>
            </div>
            {/* Inside the builder's provider, for Build with AI. */}
            <AppCommands open={jumping} onOpenChange={setJumping} mode={mode} onModeChange={changeMode} />
            {/* The one Waiting on you drawer (F457): the rail's row, the phone bar's, and an
                unanswered question in the bell all open it. */}
            <WaitingOnYouHost />
            </NavFlagsProvider>
            </HelpProvider>
            <BuilderPanel />
            </BuilderProvider>
            </TemplateSourcesProvider>
            </TooltipProvider>
            </NotificationProvider>
            </CommandRegistryProvider>
            </CanvasModeProvider>
        </NavGuardProvider>
    );
}
