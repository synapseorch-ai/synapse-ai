"use client";
/*
 * Settings' own list of tabs, beside the tab under the page frame's header.
 *
 * It used to be a full-height column left of the page with a header band of
 * its own ("Settings / Workspace configuration."), which pushed every settings
 * title 224px right of every other page's (x = 512). Now Settings is a page
 * like the rest: "Settings" is the eyebrow over the tab's title, and this list
 * is the frame's `aside` (M1a). Its groups, its flags and its collapse control
 * are what they were; below `lg` it is still the strip of links on top.
 */
import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { PanelLeftClose, PanelLeftOpen } from 'lucide-react';

import { Label, usePersisted } from '@/components/ui';
import { SETTINGS_NAV, visibleItems } from '@/lib/nav';
import { cn } from '@/lib/utils';
import { useNavFlags } from './NavFlags';

export function SettingsNav() {
    const pathname = usePathname();
    const flags = useNavFlags();
    const [collapsedFlag, setCollapsed] = usePersisted<'0' | '1'>(
        'synapseSettingsNavCollapsed', raw => (raw === '1' ? '1' : '0'), '0');
    const collapsed = collapsedFlag === '1';

    return (
        <>
            <nav
                aria-label="Settings"
                className={cn('hidden flex-col lg:flex', collapsed ? 'w-10' : 'w-52')}
            >
                {SETTINGS_NAV.map(({ group, items }, gi) => {
                    const shown = visibleItems(items, flags);
                    if (shown.length === 0) return null;
                    // The collapse control sits on the first group's heading line (Naveen, M2g),
                    // not on a row of its own above it.
                    const toggle = gi === 0 ? (
                        <button
                            onClick={() => setCollapsed(collapsed ? '0' : '1')}
                            title={collapsed ? 'Expand settings menu' : 'Collapse settings menu'}
                            aria-label={collapsed ? 'Expand settings menu' : 'Collapse settings menu'}
                            aria-expanded={!collapsed}
                            className={cn(
                                'flex size-7 shrink-0 items-center justify-center rounded-md text-text-muted transition-colors hover:bg-surface-2 hover:text-text',
                                collapsed && 'mx-auto mb-2',
                            )}
                        >
                            {collapsed
                                ? <PanelLeftOpen className="h-4 w-4" />
                                : <PanelLeftClose className="h-4 w-4" />}
                        </button>
                    ) : null;
                    return (
                        <div key={group} className="mb-4 last:mb-0">
                            {collapsed ? (
                                toggle ?? <div className="mx-1 mb-2 border-t border-border" />
                            ) : (
                                <div className="flex items-center justify-between gap-2 pb-1 pl-2.5">
                                    <Label className="block py-1.5">{group}</Label>
                                    {toggle}
                                </div>
                            )}
                            <div className="space-y-0.5">
                                {shown.map(item => {
                                    const active = pathname === item.href;
                                    return (
                                        <Link
                                            key={item.href}
                                            href={item.href}
                                            title={item.label}
                                            aria-current={active ? 'page' : undefined}
                                            className={cn(
                                                'relative flex h-8 items-center gap-2.5 rounded-md px-2.5 text-sm transition-colors',
                                                collapsed && 'justify-center px-0',
                                                active
                                                    ? 'bg-surface-2 text-text'
                                                    : 'text-text-muted hover:bg-surface hover:text-text',
                                            )}
                                        >
                                            {active && !collapsed && (
                                                <span aria-hidden className="absolute inset-y-1 left-0 w-0.5 rounded-full bg-accent" />
                                            )}
                                            <item.icon className="h-4 w-4 shrink-0" />
                                            {!collapsed && <span className="truncate">{item.label}</span>}
                                        </Link>
                                    );
                                })}
                            </div>
                        </div>
                    );
                })}
            </nav>

            {/* Narrow screens get a horizontal strip instead of the column. */}
            <div className="-mx-4 flex gap-1 overflow-x-auto px-4 md:-mx-10 md:px-10 lg:hidden">
                {SETTINGS_NAV.flatMap(g => visibleItems(g.items, flags)).map(item => {
                    const active = pathname === item.href;
                    return (
                        <Link
                            key={item.href}
                            href={item.href}
                            aria-current={active ? 'page' : undefined}
                            className={cn(
                                'flex h-8 shrink-0 items-center gap-2 rounded-md px-2.5 text-sm transition-colors',
                                active
                                    ? 'bg-surface-2 text-text'
                                    : 'text-text-muted hover:bg-surface hover:text-text',
                            )}
                        >
                            <item.icon className="h-4 w-4 shrink-0" />
                            {item.label}
                        </Link>
                    );
                })}
            </div>
        </>
    );
}
