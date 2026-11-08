"use client";
/*
 * The persistent left rail: D209's hubs with the Vault on its own (M1b), on the
 * frame colour beside the page's sheet.
 *
 * Every hub comes from HUB_NAV; there is no second copy of the list. A hub
 * stands for its tabs: it links to the first one shown and lights up on any of
 * them. Above the hubs sit the two things that are not destinations but are
 * reached from every page — Jump to… (⌘K) and Build with AI. The header is the
 * mark, the name and the collapse control, as in cloud's rail; the foot holds
 * Settings, and a single "More" menu (the theme, the docs, the changelog, and
 * Sign out when login is on) with the bell beside it, as cloud's account row has.
 *
 * On a wide screen it collapses to icons, by choice (persisted). On a phone it
 * is not a strip at all: the shell's top bar opens it as a drawer (`variant`).
 */
import Link from 'next/link';
import { usePathname, useRouter } from 'next/navigation';
import { useEffect, useState } from 'react';
import {
    BookOpen, LifeBuoy, LogOut, Megaphone, Monitor, Moon, MoreHorizontal, PanelLeftClose,
    PanelLeftOpen, Search, Settings, Sun, UserRound,
} from 'lucide-react';

import {
    Label, Mark, Menu, MenuItem, MenuLabel, MenuLink, MenuSegment, MenuSeparator, useIsCanvasMode, usePersisted,
} from '@/components/ui';
import { AUTH_CHANGED_EVENT, canSignOut, readAuthStatus } from '@/lib/auth';
import { HUB_NAV, isAtHref, visibleTabs, type NavHub } from '@/lib/nav';
import { useNotifications } from '@/components/notifications/NotificationProvider';
import { NotificationBell } from '@/components/notifications/NotificationBell';
import { useBuilder } from './BuilderProvider';
import { useNavFlags } from './NavFlags';
import { useNavGuard } from './NavGuard';
import { useWaitingOnYou } from './WaitingOnYou';
import { cn } from '@/lib/utils';
import type { ThemeMode } from '@/lib/theme';

export type { ThemeMode };

const SETTINGS_HREF = '/settings/general';
const DOCS_URL = 'https://docs.synapseorch.com';
const CHANGELOG_URL = 'https://synapseorch.com/changelog/';

const THEME_OPTIONS: { value: ThemeMode; label: string; icon: typeof Sun }[] = [
    { value: 'system', label: 'System theme', icon: Monitor },
    { value: 'light', label: 'Light theme', icon: Sun },
    { value: 'dark', label: 'Dark theme', icon: Moon },
];

/** One row: 36px, an icon slot, a label that hides with the rail. */
const ROW = 'relative flex h-9 items-center gap-2.5 rounded-md px-2.5 text-sm transition-colors';
/** A row at rest on the frame, and under the pointer: the frame's own hover (D223), not a grey on the blue. */
const ROW_IDLE = 'text-text-muted hover:bg-[var(--frame-hover)] hover:text-text';
/**
 * The page you are on: a piece of the sheet's ground with a hairline, so the
 * row reads as the same surface as the page it opened (Prototype 4).
 */
const ROW_CURRENT = 'bg-bg text-text shadow-sm ring-1 ring-border';

/** The label of a row: read by a screen reader even while the rail shows icons only. */
function RowLabel({ collapsed, children }: { collapsed: boolean; children: React.ReactNode }) {
    return <span className={collapsed ? 'sr-only' : 'truncate'}>{children}</span>;
}

function HubLink({
    hub, tabs, href, active, badge, collapsed, onNavigate,
}: {
    hub: NavHub;
    /** The hub's tabs shown here: named in its tooltip, so Evals and Usage are found under Runs (GUIDE1, F914). */
    tabs: { label: string }[];
    href: string;
    active: boolean;
    badge?: number;
    collapsed: boolean;
    onNavigate: (e: React.MouseEvent, href: string) => void;
}) {
    const Icon = hub.icon;
    return (
        <Link
            href={href}
            onClick={e => onNavigate(e, href)}
            aria-current={active ? 'page' : undefined}
            title={tabs.length > 1 ? `${hub.label}: ${tabs.map(t => t.label).join(' · ')}` : hub.label}
            data-tour={`rail-${hub.id}`}
            className={cn(ROW, collapsed && 'justify-center', active ? ROW_CURRENT : ROW_IDLE)}
        >
            <Icon className={cn('size-4 shrink-0', active ? 'text-text' : 'text-text-faint')} aria-hidden />
            <RowLabel collapsed={collapsed}>{hub.label}</RowLabel>
            {badge ? (
                <span className={cn(
                    'flex h-4 min-w-[16px] items-center justify-center rounded-full',
                    'bg-accent px-1 text-2xs font-semibold text-accent-fg',
                    collapsed ? 'absolute right-1 top-1' : 'ml-auto',
                )}>
                    {badge > 9 ? '9+' : badge}
                </span>
            ) : null}
        </Link>
    );
}

export function AppRail({
    mode, onModeChange, variant = 'side', showBell = true, onJump, jumpHint,
}: {
    mode: ThemeMode;
    onModeChange: (mode: ThemeMode) => void;
    /** `side` sits beside the sheet on a wide screen; `drawer` fills the phone menu. */
    variant?: 'side' | 'drawer';
    /** Off where the phone's top bar holds the bell instead: two bells is one too many. */
    showBell?: boolean;
    /** Opens ⌘K. The Jump to… row shows only when there is a palette to open. */
    onJump?: () => void;
    /** The shortcut as this keyboard writes it: "⌘K" or "Ctrl K". */
    jumpHint?: string;
}) {
    const pathname = usePathname();
    const router = useRouter();
    const { unseenCount } = useNotifications();
    const { open: openBuilder, streaming, state } = useBuilder();
    const flags = useNavFlags();
    const mayLeave = useNavGuard();
    const [collapsedFlag, setCollapsedFlag] = usePersisted<'0' | '1'>(
        'synapseRailCollapsed', raw => (raw === '1' ? '1' : '0'), '0');
    // On an open canvas the menu folds to icons, and comes back as it was when
    // the canvas closes (D204; Naveen's default at M3a's plan). Expanding it there
    // holds for that visit only: the saved setting is never written from a canvas.
    const canvas = useIsCanvasMode();
    const [openOnCanvas, setOpenOnCanvas] = useState(false);
    const [seenCanvas, setSeenCanvas] = useState(canvas);
    if (seenCanvas !== canvas) {
        setSeenCanvas(canvas);
        setOpenOnCanvas(false);
    }
    const collapsed = variant === 'side' && (canvas ? !openOnCanvas : collapsedFlag === '1');
    const setCollapsed = (next: '0' | '1') => (canvas ? setOpenOnCanvas(next === '0') : setCollapsedFlag(next));
    // A question waiting on the person (F214) shows here: the menu's row is the
    // way into the builder since the floating launcher was retired (M2c).
    const waiting = state.pending !== null;
    // Runs and tool calls waiting on the person, from the bell's stream (M2c).
    const waitingOnYou = useWaitingOnYou();
    const { openWaiting } = useNotifications();
    const showWaiting = () => {
        // On Home the list is already the page's first section.
        const section = pathname === '/dashboard' ? document.getElementById('home-needs-you') : null;
        if (section) {
            section.scrollIntoView({ behavior: 'smooth', block: 'start' });
            return;
        }
        openWaiting();
    };

    // Sign out is offered only where there is a password to sign back in with
    // (F221). Read again when Settings turns login on or off.
    const [signOutShown, setSignOutShown] = useState(false);
    useEffect(() => {
        let live = true;
        const read = () => readAuthStatus().then(s => { if (live) setSignOutShown(canSignOut(s)); });
        read();
        window.addEventListener(AUTH_CHANGED_EVENT, read);
        return () => { live = false; window.removeEventListener(AUTH_CHANGED_EVENT, read); };
    }, []);

    const signOut = async () => {
        if (!mayLeave()) return;
        try {
            await fetch('/api/auth/logout', { method: 'POST' });
        } finally {
            // A full navigation: nothing of the signed-in app stays in memory.
            window.location.assign('/login');
        }
    };

    // Chat registers a guard while a run is streaming; honour it before moving.
    const onNavigate = (e: React.MouseEvent, href: string) => {
        e.preventDefault();
        if (pathname === href) return;   // already here; don't re-render
        if (!mayLeave()) return;
        router.push(href);
    };

    return (
        <nav
            aria-label="Primary"
            className={cn(
                'flex shrink-0 flex-col bg-frame',
                variant === 'drawer'
                    ? 'h-full w-full'
                    : cn('transition-[width] duration-150', collapsed ? 'w-14' : 'w-60'),
            )}
        >
            {/* The three-layer mark, never on a tile, beside the product's name
                (D205), and the collapse control beside them — cloud's header
                (Naveen, 2026-09-13; and for OSS in M1a). One control per header:
                collapsed there is room for one 36px button, so the mark is it,
                and it turns into the expand icon under the pointer or focus.

                `compact={false}` because 20 is exactly `COMPACT_BELOW`: at this
                size the full mark reads, but one digit off and it would silently
                become the reduced two-layer form. Say which mark this is rather
                than letting the size decide. */}
            <div className={cn(
                'flex h-14 shrink-0 items-center',
                collapsed ? 'justify-center px-2' : 'gap-2.5 pl-4 pr-2',
            )}>
                {collapsed ? (
                    <button
                        type="button"
                        onClick={() => setCollapsed('0')}
                        aria-expanded={false}
                        aria-label="Expand sidebar"
                        title="Expand sidebar"
                        className="group/expand relative flex size-9 items-center justify-center rounded-md transition-colors hover:bg-[var(--frame-hover)]"
                    >
                        <Mark
                            size={20}
                            compact={false}
                            className="text-accent transition-opacity group-hover/expand:opacity-0 group-focus-visible/expand:opacity-0"
                        />
                        <PanelLeftOpen
                            className="absolute size-4 text-text-muted opacity-0 transition-opacity group-hover/expand:opacity-100 group-focus-visible/expand:opacity-100"
                            aria-hidden
                        />
                    </button>
                ) : (
                    <>
                        <Mark size={20} compact={false} className="shrink-0 text-accent" />
                        <span className="min-w-0 flex-1 truncate text-sm font-semibold tracking-tight text-text">
                            SynapseOrch
                        </span>
                        {/* A phone's menu is a drawer that closes itself, so it has nothing to collapse. */}
                        {variant === 'side' && (
                            <button
                                type="button"
                                onClick={() => setCollapsed('1')}
                                aria-expanded
                                aria-label="Collapse sidebar"
                                title="Collapse sidebar"
                                className={cn('flex size-9 shrink-0 items-center justify-center rounded-md', ROW_IDLE)}
                            >
                                <PanelLeftClose className="size-4" aria-hidden />
                            </button>
                        )}
                    </>
                )}
            </div>

            {/* Reached from every page, so they stand above the hubs rather
                than inside one. 12px apart, not the hubs' 4px: two controls,
                not two rows of one list (Naveen, M1b) — the same 12px as from
                Build with AI down to Home. */}
            <div className="shrink-0 space-y-3 px-2 pb-2">
                {onJump && (
                    <button
                        type="button"
                        onClick={onJump}
                        title={`Jump to… ${jumpHint ?? ''}`.trim()}
                        className={cn(
                            ROW, 'w-full border border-border bg-bg text-text-faint hover:border-border-strong hover:text-text-muted',
                            collapsed && 'justify-center',
                        )}
                    >
                        <Search className="size-4 shrink-0" aria-hidden />
                        <RowLabel collapsed={collapsed}>Jump to…</RowLabel>
                        {!collapsed && jumpHint && (
                            <kbd className="ml-auto rounded border border-border px-1.5 font-mono text-2xs text-text-faint">
                                {jumpHint}
                            </kbd>
                        )}
                    </button>
                )}
                <button
                    type="button"
                    onClick={() => openBuilder()}
                    title="Synapse AI"
                    data-testid="assistant-open"
                    className={cn(
                        // The frame's own action (D223): a white card in light, teal in dark.
                        ROW, 'w-full bg-[var(--frame-action)] font-medium text-accent-ink shadow-[var(--frame-action-shadow)]',
                        'hover:bg-[var(--frame-action-hover)]',
                        collapsed && 'justify-center',
                    )}
                >
                    {/* The mark wherever the product acts (feedback-app-shell-design). */}
                    <Mark size={20} compact={false} className="shrink-0 text-accent" aria-hidden />
                    <RowLabel collapsed={collapsed}>Synapse AI</RowLabel>
                    {waiting && <span className="sr-only">(waiting for your answer)</span>}
                    {(streaming || waiting) && (
                        <span
                            aria-hidden
                            className={cn(
                                'absolute size-2 rounded-full',
                                waiting ? 'bg-person' : 'bg-machine',
                                collapsed ? 'right-1 top-1' : 'right-3 top-1/2 -translate-y-1/2',
                            )}
                        />
                    )}
                </button>
                {/* What waits on a person, from any screen (M2c): amber, and only while something does. */}
                {waitingOnYou.count > 0 && (
                    <button
                        type="button"
                        onClick={showWaiting}
                        title={`Waiting on you · ${waitingOnYou.count}`}
                        className={cn(
                            ROW, 'w-full bg-person-subtle font-medium text-person-ink',
                            'hover:bg-[color-mix(in_oklab,var(--person-subtle)_80%,var(--person))]',
                            collapsed && 'justify-center',
                        )}
                    >
                        <UserRound className="size-4 shrink-0" aria-hidden />
                        <RowLabel collapsed={collapsed}>Waiting on you</RowLabel>
                        <span
                            className={cn(
                                'rounded-full bg-person px-1.5 text-2xs font-semibold tabular-nums text-person-fg',
                                collapsed ? 'absolute right-0.5 top-0.5 min-w-4 text-center' : 'ml-auto',
                            )}
                        >
                            {waitingOnYou.count > 9 ? '9+' : waitingOnYou.count}
                        </span>
                    </button>
                )}
            </div>

            {/* `pt-1`: a scroller clips at its own edge, and the current row's
                hairline and shadow sit just outside the row — on Home, the first
                row, its top was cut off. */}
            <div className="flex-1 overflow-y-auto px-2 pb-2 pt-1">
                {HUB_NAV.map(({ group, hubs }, groupIndex) => {
                    const shown = hubs
                        .map(hub => ({ hub, tabs: visibleTabs(hub, flags) }))
                        .filter(({ tabs }) => tabs.length > 0);
                    if (shown.length === 0) return null;
                    return (
                        <div key={group ?? 'root'} className="mb-4 last:mb-0">
                            {group && !collapsed && (
                                <Label className="block px-2.5 pb-1.5 pt-1">{group}</Label>
                            )}
                            {/* Collapsed, the labels are hidden, so a rule is what
                                keeps the groups from reading as one long list. */}
                            {groupIndex > 0 && collapsed && (
                                <div className="mx-2 mb-2 border-t border-border" />
                            )}
                            <div className="space-y-1">
                                {shown.map(({ hub, tabs }) => (
                                    <HubLink
                                        key={hub.id}
                                        hub={hub}
                                        tabs={tabs}
                                        href={tabs[0].href}
                                        active={hub.tabs.some(t => isAtHref(pathname, t.href))}
                                        badge={hub.badge === 'unseenRuns' ? unseenCount : undefined}
                                        collapsed={collapsed}
                                        onNavigate={onNavigate}
                                    />
                                ))}
                            </div>
                        </div>
                    );
                })}
            </div>

            <div className="shrink-0 space-y-1 p-2">
                <Link
                    href={SETTINGS_HREF}
                    onClick={e => onNavigate(e, SETTINGS_HREF)}
                    aria-current={pathname.startsWith('/settings') ? 'page' : undefined}
                    title="Settings"
                    className={cn(
                        ROW, collapsed && 'justify-center',
                        pathname.startsWith('/settings') ? ROW_CURRENT : ROW_IDLE,
                    )}
                >
                    <Settings className="size-4 shrink-0" aria-hidden />
                    <RowLabel collapsed={collapsed}>Settings</RowLabel>
                </Link>
                {/* Everything that is not a destination lives behind one row:
                    the theme, the docs, the changelog. The bell is its sibling,
                    not its child — inside, opening notifications would open the
                    menu — beside it while the rail is open, below it collapsed
                    (56px fits one 36px control per row), as in cloud. */}
                <div className={cn('flex gap-1', collapsed ? 'flex-col items-center' : 'items-center')}>
                    <Menu
                        side="top"
                        align="start"
                        sideOffset={8}
                        trigger={
                            <button
                                type="button"
                                title="More"
                                className={cn(
                                    ROW, ROW_IDLE,
                                    'data-[state=open]:bg-[var(--frame-hover)] data-[state=open]:text-text',
                                    collapsed ? 'w-full justify-center' : 'min-w-0 flex-1',
                                )}
                            >
                                <MoreHorizontal className="size-4 shrink-0" aria-hidden />
                                <RowLabel collapsed={collapsed}>More</RowLabel>
                            </button>
                        }
                    >
                        {/* Settings here as well as on the rail (F904, Naveen): the menu is where people look for it. */}
                        <MenuLink href={SETTINGS_HREF} icon={Settings} onClick={e => onNavigate(e, SETTINGS_HREF)}>Settings</MenuLink>
                        <MenuSeparator />
                        <MenuSegment label="Theme" value={mode} options={THEME_OPTIONS} onChange={onModeChange} />
                        <MenuSeparator />
                        <MenuLabel>Help</MenuLabel>
                        <MenuLink href={DOCS_URL} icon={BookOpen} external>Documentation</MenuLink>
                        <MenuLink href="/settings/support" icon={LifeBuoy} onClick={e => onNavigate(e, '/settings/support')}>Help &amp; support</MenuLink>
                        <MenuLink href={CHANGELOG_URL} icon={Megaphone} external>What&apos;s new</MenuLink>
                        {signOutShown && (
                            <>
                                <MenuSeparator />
                                <MenuItem icon={LogOut} onSelect={signOut}>Sign out</MenuItem>
                            </>
                        )}
                    </Menu>
                    {showBell && <NotificationBell side="right" align="end" />}
                </div>
            </div>
        </nav>
    );
}
