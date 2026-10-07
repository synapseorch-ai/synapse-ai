'use client';
/*
 * A hub's tab bar in OSS: the kit's `HubTabs`, fed from lib/nav.ts (M1a).
 *
 * It hides a tab whose settings flag is off, as the rail does — except the one
 * being shown, which is reachable by its address whatever the flag says. A
 * click honours chat's guard like the rail's links; AgentsScreen's unsaved-
 * edits guard catches it on its own, since these are real links.
 */
import { usePathname, useRouter } from 'next/navigation';

import { HubTabs } from '@/components/ui';
import { HUB_NAV, visibleTabs } from '@/lib/nav';
import { useNavFlags } from './NavFlags';
import { useNavGuard } from './NavGuard';

export function AppHubTabs({ hubId, current }: { hubId: string; current: string }) {
    const router = useRouter();
    const pathname = usePathname();
    const flags = useNavFlags();
    const mayLeave = useNavGuard();
    const hub = HUB_NAV.flatMap(g => g.hubs).find(h => h.id === hubId);
    if (!hub) return null;
    const shown = new Set(visibleTabs(hub, flags).map(t => t.href));
    const tabs = hub.tabs
        .filter(t => shown.has(t.href) || t.href === current)
        .map(t => ({ href: t.href, label: t.label }));

    const onNavigate = (event: React.MouseEvent, href: string) => {
        // A modified click opens a new tab or window: leave it to the browser.
        if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0) return;
        event.preventDefault();
        if (href === pathname) return;
        if (!mayLeave()) return;
        router.push(href);
    };

    return <HubTabs label={hub.label} tabs={tabs} current={current} onNavigate={onNavigate} />;
}
