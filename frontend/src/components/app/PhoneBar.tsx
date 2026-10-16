'use client';
/*
 * The phone's menu (M1a): a slim bar at the top of the sheet, and the rail as a
 * drawer behind its ☰.
 *
 * Below `md` a 56px icon strip used to take a seventh of a 400px screen on
 * every page, for icons with no names. Now the page gets the width, and the
 * whole rail — names, groups, Settings, More — is one tap away (Prototype 4).
 * The drawer is a Radix dialog, so focus moves into it and back, Tab stays
 * inside, Escape and the scrim close it; it also closes on arriving at a page.
 */
import * as RDialog from '@radix-ui/react-dialog';
import { Menu as MenuIcon, UserRound, X } from 'lucide-react';
import { usePathname } from 'next/navigation';
import { useState, useSyncExternalStore } from 'react';

import { IconButton, Mark } from '@/components/ui';
import { NotificationBell } from '@/components/notifications/NotificationBell';
import { hubForPath } from '@/lib/nav';
import { AppRail, type ThemeMode } from './AppRail';
import { useBuilder } from './BuilderProvider';
import { useNotifications } from '@/components/notifications/NotificationProvider';
import { useWaitingOnYou } from './WaitingOnYou';

const PHONE = '(max-width: 767px)';

/**
 * True below `md`. False on the server and in the first client render, so the
 * hydrated HTML matches; the bar itself is hidden by CSS above `md` either way.
 */
export function useIsPhone(): boolean {
    return useSyncExternalStore(
        (notify) => {
            const media = window.matchMedia(PHONE);
            media.addEventListener('change', notify);
            return () => media.removeEventListener('change', notify);
        },
        () => window.matchMedia(PHONE).matches,
        () => false,
    );
}

export function PhoneBar({
    mode, onModeChange, onJump, jumpHint,
}: {
    mode: ThemeMode;
    onModeChange: (mode: ThemeMode) => void;
    onJump?: () => void;
    jumpHint?: string;
}) {
    const pathname = usePathname();
    const isPhone = useIsPhone();
    // The page the menu was opened on. Arriving at another page closes the menu
    // that took you there, and widening the window past `md` puts the rail back
    // beside the sheet — both without an effect, as a fact derived from state.
    const [openOn, setOpenOn] = useState<string | null>(null);
    const open = isPhone && openOn === pathname;
    const setOpen = (next: boolean) => setOpenOn(next ? pathname : null);

    // What waits on the person, beside the bell (M2c): on a phone the menu's row is a tap away, this is not.
    const waiting = useWaitingOnYou();
    // The mark opens the AI builder: one tap, as the retired corner launcher was (M2c).
    const { open: openBuilder, streaming, state } = useBuilder();
    const builderWaiting = state.pending !== null;
    const { openWaiting } = useNotifications();

    const here = hubForPath(pathname)?.hub.label
        ?? (pathname.startsWith('/settings') ? 'Settings' : 'SynapseOrch');

    return (
        <div className="flex h-12 shrink-0 items-center gap-2 border-b border-border px-2 md:hidden">
            <RDialog.Root open={open} onOpenChange={setOpen}>
                <RDialog.Trigger asChild>
                    <IconButton label="Open the menu" icon={MenuIcon} />
                </RDialog.Trigger>
                <RDialog.Portal>
                    <RDialog.Overlay className="fixed inset-0 z-50 bg-scrim/40 md:hidden" />
                    <RDialog.Content
                        aria-describedby={undefined}
                        className="fixed inset-y-0 left-0 z-50 flex w-[17rem] max-w-[85vw] flex-col bg-frame shadow-elevated md:hidden"
                    >
                        <RDialog.Title className="sr-only">Menu</RDialog.Title>
                        <div className="absolute right-2 top-2.5 z-10">
                            <RDialog.Close asChild>
                                <IconButton label="Close the menu" icon={X} size="sm" />
                            </RDialog.Close>
                        </div>
                        {/* The top bar keeps the bell, so the drawer's rail has none. */}
                        <AppRail
                            mode={mode}
                            onModeChange={onModeChange}
                            variant="drawer"
                            showBell={false}
                            onJump={onJump ? () => { setOpen(false); onJump(); } : undefined}
                            jumpHint={jumpHint}
                        />
                    </RDialog.Content>
                </RDialog.Portal>
            </RDialog.Root>
            <button
                type="button"
                onClick={() => openBuilder()}
                aria-label={builderWaiting ? 'Synapse AI (waiting for your answer)' : 'Synapse AI'}
                title="Synapse AI"
                className="relative flex size-9 shrink-0 items-center justify-center rounded-md hover:bg-surface-2"
            >
                <Mark size={20} compact={false} className="text-accent" aria-hidden />
                {(streaming || builderWaiting) && (
                    <span aria-hidden className={`absolute right-1 top-1 size-2 rounded-full ${builderWaiting ? 'bg-person' : 'bg-machine'}`} />
                )}
            </button>
            <span className="min-w-0 truncate text-sm font-semibold tracking-tight text-text">{here}</span>
            {/* One bell in the page at a time: the rail's is hidden on a phone. */}
            {isPhone && (
                <div className="ml-auto flex shrink-0 items-center gap-1">
                    {waiting.count > 0 && (
                        <button
                            type="button"
                            onClick={() => openWaiting()}
                            aria-label={`Waiting on you · ${waiting.count}`}
                            className="inline-flex h-8 items-center gap-1.5 rounded-full bg-person-subtle px-2.5 text-xs font-semibold text-person-ink"
                        >
                            <UserRound className="size-3.5" aria-hidden />
                            {waiting.count > 9 ? '9+' : waiting.count}
                        </button>
                    )}
                    <NotificationBell />
                </div>
            )}
        </div>
    );
}
