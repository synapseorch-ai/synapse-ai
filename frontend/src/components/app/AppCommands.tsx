'use client';
/*
 * ⌘K in OSS: the kit's `CommandPalette`, with this product's list (M1a).
 *
 * - Go to: every hub's tabs and every settings tab, each with where it lives
 *   ("In Runs › Logs"), from lib/nav.ts — so a page added there is findable here
 * - Actions: Build with AI, a new workflow, and the two themes not in force
 * - Workflows and agents by name, read when the palette opens
 *
 * Every way out honours chat's guard, and goes through a real link click, so
 * AgentsScreen's unsaved-edits guard (a capture-phase listener on links) still
 * asks before an edit is lost.
 */
import { useRouter } from 'next/navigation';
import { useEffect, useState } from 'react';
import { Bot, Monitor, Moon, Plus, Sun, Workflow } from 'lucide-react';

import { CommandPalette, Mark, type CommandGroup } from '@/components/ui';
import { HUB_NAV, SETTINGS_NAV, visibleItems, visibleTabs } from '@/lib/nav';
import { useAppliedTheme, type ThemeMode } from '@/lib/theme';
import { useBuilder } from './BuilderProvider';
import { useNavFlags } from './NavFlags';
import { useNavGuard } from './NavGuard';

type Named = { id: string; name: string; description?: string | null };

const ICON = 'size-4';

export function AppCommands({
    open, onOpenChange, mode, onModeChange,
}: {
    open: boolean;
    onOpenChange: (open: boolean) => void;
    mode: ThemeMode;
    onModeChange: (mode: ThemeMode) => void;
}) {
    const router = useRouter();
    const flags = useNavFlags();
    const mayLeave = useNavGuard();
    const { open: openBuilder } = useBuilder();
    const [workflows, setWorkflows] = useState<Named[]>([]);
    const [agents, setAgents] = useState<Named[]>([]);

    // Names are read each time it opens: a workflow made a minute ago is findable.
    useEffect(() => {
        if (!open) return;
        let live = true;
        const list = (path: string) => fetch(path).then(r => r.json()).then(d => (Array.isArray(d) ? d as Named[] : []));
        list('/api/orchestrations').then(d => { if (live) setWorkflows(d); }).catch(() => { });
        list('/api/agents')
            .then(d => { if (live) setAgents(d.filter(a => !a.id.startsWith('agent_native_builder'))); })
            .catch(() => { });
        return () => { live = false; };
    }, [open]);

    const go = (href: string) => {
        if (!mayLeave()) return;
        // A link click, so the page's own guards see it as one. If none of them
        // took the navigation over, it is a client-side push, not a reload.
        const link = document.createElement('a');
        link.href = href;
        link.addEventListener('click', (event) => {
            if (event.defaultPrevented) return;
            event.preventDefault();
            router.push(href);
        });
        document.body.appendChild(link);
        link.click();
        link.remove();
    };

    const pages: CommandGroup = {
        heading: 'Go to',
        items: HUB_NAV.flatMap(({ group, hubs }) => hubs.flatMap(hub => {
            const tabs = visibleTabs(hub, flags);
            return tabs.map(tab => {
                const Icon = tab.icon;
                return {
                    id: `page:${tab.href}`,
                    label: tab.label,
                    hint: hub.tabs.length > 1 && tab.label !== hub.label ? `In ${hub.label} › ${tab.label}` : undefined,
                    icon: <Icon className={ICON} aria-hidden />,
                    keywords: [hub.label, group ?? 'Overview'],
                    onSelect: () => go(tab.href),
                };
            });
        })),
    };
    const settings: CommandGroup = {
        heading: 'Settings',
        items: SETTINGS_NAV.flatMap(({ group, items }) => visibleItems(items, flags).map(item => {
            const Icon = item.icon;
            return {
                id: `page:${item.href}`,
                label: item.label,
                hint: `In Settings › ${group}`,
                icon: <Icon className={ICON} aria-hidden />,
                keywords: ['Settings', group ?? ''],
                onSelect: () => go(item.href),
            };
        })),
    };

    // The theme on the page, read from the attribute the shell set: render-safe on
    // the server, where `resolveTheme` (matchMedia) is not.
    const applied = useAppliedTheme();
    const themes: { mode: ThemeMode; label: string; icon: typeof Sun }[] = [
        { mode: 'light', label: 'Switch to the light theme', icon: Sun },
        { mode: 'dark', label: 'Switch to the dark theme', icon: Moon },
        { mode: 'system', label: 'Follow the system theme', icon: Monitor },
    ];
    const actions: CommandGroup = {
        heading: 'Actions',
        items: [
            {
                id: 'action:build',
                label: 'Build with AI',
                hint: 'Describe a job and the builder makes the workflow',
                // Never under 20px, or the mark turns into its two-dot form (COMPACT_BELOW).
                icon: <Mark size={20} compact={false} className="text-accent" aria-hidden />,
                keywords: ['builder', 'AI', 'create'],
                onSelect: () => openBuilder(),
            },
            {
                id: 'action:new-workflow',
                label: 'New workflow',
                hint: 'A blank workflow on the canvas',
                icon: <Plus className={ICON} aria-hidden />,
                keywords: ['create', 'orchestration'],
                // A fresh value each time, so asking twice from /workflows asks twice.
                onSelect: () => go(`/workflows?new=${Date.now()}`),
            },
            ...themes
                .filter(t => t.mode !== mode && !(t.mode === applied && mode !== 'system'))
                .map(t => {
                    const Icon = t.icon;
                    return {
                        id: `action:theme-${t.mode}`,
                        label: t.label,
                        icon: <Icon className={ICON} aria-hidden />,
                        keywords: ['theme', 'appearance', 'dark mode', 'light mode'],
                        onSelect: () => onModeChange(t.mode),
                    };
                }),
        ],
    };
    const named = (heading: string, list: Named[], icon: React.ReactNode, href: (id: string) => string): CommandGroup => ({
        heading,
        items: list.map(item => ({
            id: `${heading}:${item.id}`,
            label: item.name || item.id,
            hint: item.description || undefined,
            icon,
            onSelect: () => go(href(item.id)),
        })),
    });

    return (
        <CommandPalette
            open={open}
            onOpenChange={onOpenChange}
            groups={[
                pages,
                actions,
                named('Workflows', workflows, <Workflow className={ICON} aria-hidden />, id => `/workflows?id=${encodeURIComponent(id)}`),
                named('Agents', agents, <Bot className={ICON} aria-hidden />, id => `/agents?id=${encodeURIComponent(id)}`),
                settings,
            ]}
        />
    );
}
