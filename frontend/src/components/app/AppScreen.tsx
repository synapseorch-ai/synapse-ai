/*
 * A rail page's frame, from its nav entry (M1a).
 *
 * Every page says the same things in the same place: the menu group it sits in
 * (the eyebrow), its name and its one-line blurb. A page that is one of a hub's
 * tabs (D209) is titled by its hub, with the hub's tab bar under the title, so
 * Runs, Logs, Evals and Usage read as one place. The words come from
 * `lib/nav.ts`, so a rename lands in the rail and the page together.
 *
 * No "use client": the route pages are server components, and this renders no
 * hooks.
 */
import { PageFrame, type PageWidth } from '@/components/ui';
import { hubOf } from '@/lib/nav';
import { AppHubTabs } from './AppHubTabs';

export function AppScreen({
    id, actions, bleed, width, children,
}: {
    /** The page's nav id (`orchestrations`, `logs`…). */
    id: string;
    actions?: React.ReactNode;
    bleed?: boolean;
    width?: PageWidth;
    children: React.ReactNode;
}) {
    const found = hubOf(id);
    if (!found) throw new Error(`AppScreen: no rail page has the id "${id}"`);
    const { group, hub, entry } = found;
    const tabbed = hub.tabs.length > 1;
    return (
        <PageFrame
            eyebrow={group ?? 'Overview'}
            title={tabbed ? hub.label : entry.label}
            description={entry.blurb}
            tabs={tabbed ? <AppHubTabs hubId={hub.id} current={entry.href} /> : undefined}
            actions={actions}
            bleed={bleed}
            width={width}
        >
            {children}
        </PageFrame>
    );
}
