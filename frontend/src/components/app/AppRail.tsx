"use client";
/*
 * The persistent left rail.
 *
 * Three groups — Overview, Build, Tools — read top to bottom as *watch, author,
 * connect*. Every entry comes from PRIMARY_NAV; there is no second copy of the
 * list. The bell sits in the header beside the mark, the one control in the
 * rail that changes on its own; the foot holds Settings and a single "More"
 * menu for the things that are not destinations — the theme, the docs, the
 * changelog — instead of a stack of one-line buttons.
 *
 * Collapses to icons, by choice (persisted) or because the viewport is narrow.
 * Under `md` it is always collapsed — a 240px rail on a phone leaves nothing
 * for the page.
 */
import Link from 'next/link';
import { usePathname, useRouter } from 'next/navigation';
import {
    BookOpen, LifeBuoy, Megaphone, Monitor, Moon, MoreHorizontal, PanelLeftClose,
    PanelLeftOpen, Settings, Sun,
} from 'lucide-react';

import {
    Label, Mark, Menu, MenuLabel, MenuLink, MenuSegment, MenuSeparator, usePersisted,
} from '@/components/ui';
import { PRIMARY_NAV, type NavEntry } from '@/lib/nav';
import { useNotifications } from '@/components/notifications/NotificationProvider';
import { NotificationBell } from '@/components/notifications/NotificationBell';
import { useNavGuard } from './NavGuard';
import { cn } from '@/lib/utils';

export type ThemeMode = 'system' | 'light' | 'dark';

const SETTINGS_HREF = '/settings/general';
const DOCS_URL = 'https://docs.synapseorch.com';
const CHANGELOG_URL = 'https://synapseorch.com/changelog/';

const THEME_OPTIONS: { value: ThemeMode; label: string; icon: typeof Sun }[] = [
    { value: 'system', label: 'System theme', icon: Monitor },
    { value: 'light', label: 'Light theme', icon: Sun },
    { value: 'dark', label: 'Dark theme', icon: Moon },
];

function isActive(pathname: string, href: string): boolean {
    if (href === '/') return pathname === '/';
    return pathname === href || pathname.startsWith(`${href}/`);
}

/** One row: 36px, an icon slot, a label that hides with the rail. */
const ROW = 'relative flex h-9 items-center gap-2.5 rounded-md px-2.5 text-sm transition-colors';

function RailLink({
    href, label, icon: Icon, active, badge, collapsed, onNavigate,
}: {
    href: string;
    label: string;
    icon: NavEntry['icon'];
    active: boolean;
    badge?: number;
    collapsed: boolean;
    onNavigate: (e: React.MouseEvent, href: string) => void;
}) {
    return (
        <Link
            href={href}
            onClick={e => onNavigate(e, href)}
            aria-current={active ? 'page' : undefined}
            title={label}
            className={cn(
                ROW,
                collapsed ? 'justify-center' : 'justify-center md:justify-start',
                active
                    ? 'bg-surface-2 text-text'
                    : 'text-text-muted hover:bg-surface hover:text-text',
            )}
        >
            {active && (
                <span
                    aria-hidden
                    className={cn(
                        'absolute inset-y-2 left-0 w-0.5 rounded-full bg-accent',
                        collapsed ? 'hidden' : 'hidden md:block',
                    )}
                />
            )}
            <Icon className="size-4 shrink-0" aria-hidden />
            <span className={cn('truncate', collapsed ? 'hidden' : 'hidden md:inline')}>
                {label}
            </span>
            {badge ? (
                <span className={cn(
                    'flex h-4 min-w-[16px] items-center justify-center rounded-full',
                    'bg-accent px-1 text-2xs font-semibold text-accent-fg',
                    collapsed
                        ? 'absolute right-1 top-1'
                        : 'absolute right-1 top-1 md:static md:ml-auto',
                )}>
                    {badge > 9 ? '9+' : badge}
                </span>
            ) : null}
        </Link>
    );
}

export function AppRail({
    mode, onModeChange,
}: {
    mode: ThemeMode;
    onModeChange: (mode: ThemeMode) => void;
}) {
    const pathname = usePathname();
    const router = useRouter();
    const { unseenCount } = useNotifications();
    const mayLeave = useNavGuard();
    const [collapsedFlag, setCollapsed] = usePersisted<'0' | '1'>(
        'synapseRailCollapsed', raw => (raw === '1' ? '1' : '0'), '0');
    const collapsed = collapsedFlag === '1';

    // Chat registers a guard while a run is streaming; honour it before moving.
    const onNavigate = (e: React.MouseEvent, href: string) => {
        e.preventDefault();
        if (isActive(pathname, href)) return;   // already here; don't re-render
        if (!mayLeave()) return;
        router.push(href);
    };

    return (
        <nav
            aria-label="Primary"
            className={cn(
                'flex shrink-0 flex-col border-r border-border bg-bg transition-[width] duration-150',
                collapsed ? 'w-14' : 'w-14 md:w-60',
            )}
        >
            <div className={cn(
                'flex h-14 shrink-0 items-center border-b border-border',
                collapsed ? 'justify-center px-2' : 'gap-2.5 px-4',
            )}>
                {/* The mark is the brand; the name reads beside it while there is
                    room. Collapsed there is 24px of content, and the bell —
                    the one thing here that changes on its own — takes it.

                    `compact={false}` because 20 is exactly `COMPACT_BELOW`: at
                    this size the full mark reads, but one digit off and it
                    would silently become the reduced two-layer form. Say which
                    mark this is rather than letting the size decide. */}
                <Mark size={20} compact={false} className={cn('shrink-0 text-accent', collapsed ? 'hidden' : 'hidden md:block')} />
                <span className={cn(
                    'text-sm font-semibold tracking-tight text-text',
                    collapsed ? 'hidden' : 'hidden md:inline',
                )}>
                    SynapseOrch
                </span>
                {/* The bell is the only control up here beside the name.
                    Collapse moved to the foot: three controls in a 240px
                    header left the name clipped. */}
                <div className={cn('shrink-0', collapsed ? '' : 'ml-auto')}>
                    <NotificationBell placement="below-right" />
                </div>
            </div>

            <div className="flex-1 overflow-y-auto p-2">
                {PRIMARY_NAV.map(({ group, items }, groupIndex) => (
                    <div key={group ?? 'root'} className="mb-5 last:mb-0">
                        {group && (
                            <Label className={cn(
                                'block px-2.5 pb-2 pt-1',
                                collapsed ? 'hidden' : 'hidden md:block',
                            )}>
                                {group}
                            </Label>
                        )}
                        {/* Collapsed, the labels are hidden, so a rule is what
                            keeps the groups from reading as one long list. */}
                        {groupIndex > 0 && (
                            <div className={cn(
                                'mx-2 mb-2 border-t border-border',
                                collapsed ? 'block' : 'block md:hidden',
                            )} />
                        )}
                        <div className="space-y-1">
                            {items.map(item => (
                                <RailLink
                                    key={item.href}
                                    href={item.href}
                                    label={item.label}
                                    icon={item.icon}
                                    active={isActive(pathname, item.href)}
                                    badge={item.badge === 'unseenRuns' ? unseenCount : undefined}
                                    collapsed={collapsed}
                                    onNavigate={onNavigate}
                                />
                            ))}
                        </div>
                    </div>
                ))}
            </div>

            <div className="shrink-0 space-y-1 border-t border-border p-2">
                {/* One control, one place, both directions. */}
                <button
                    type="button"
                    onClick={() => setCollapsed(collapsed ? '0' : '1')}
                    aria-expanded={!collapsed}
                    title={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
                    className={cn(
                        ROW, 'w-full text-text-muted hover:bg-surface hover:text-text',
                        collapsed ? 'justify-center' : 'justify-center md:justify-start',
                    )}
                >
                    {collapsed
                        ? <PanelLeftOpen className="size-4 shrink-0" aria-hidden />
                        : <PanelLeftClose className="size-4 shrink-0" aria-hidden />}
                    <span className={cn('truncate', collapsed ? 'hidden' : 'hidden md:inline')}>
                        Collapse
                    </span>
                </button>
                <RailLink
                    href={SETTINGS_HREF}
                    label="Settings"
                    icon={Settings}
                    active={pathname.startsWith('/settings')}
                    collapsed={collapsed}
                    onNavigate={onNavigate}
                />
                {/* Everything that is not a destination lives behind one row:
                    the theme, the docs, the changelog. */}
                <Menu
                    side="top"
                    align="start"
                    sideOffset={8}
                    trigger={
                        <button
                            type="button"
                            title="More"
                            className={cn(
                                ROW, 'w-full text-text-muted hover:bg-surface hover:text-text',
                                'data-[state=open]:bg-surface-2 data-[state=open]:text-text',
                                collapsed ? 'justify-center' : 'justify-center md:justify-start',
                            )}
                        >
                            <MoreHorizontal className="size-4 shrink-0" aria-hidden />
                            <span className={cn('truncate', collapsed ? 'hidden' : 'hidden md:inline')}>
                                More
                            </span>
                        </button>
                    }
                >
                    <MenuSegment label="Theme" value={mode} options={THEME_OPTIONS} onChange={onModeChange} />
                    <MenuSeparator />
                    <MenuLabel>Help</MenuLabel>
                    <MenuLink href={DOCS_URL} icon={BookOpen} external>Documentation</MenuLink>
                    <MenuLink href="/settings/support" icon={LifeBuoy}>Help &amp; support</MenuLink>
                    <MenuLink href={CHANGELOG_URL} icon={Megaphone} external>What&apos;s new</MenuLink>
                </Menu>
            </div>
        </nav>
    );
}
