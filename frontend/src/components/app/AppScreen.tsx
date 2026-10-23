/*
 * A rail page's frame, from its nav entry (M1a).
 *
 * Every page says the same things in the same place: the menu group it sits in
 * (the eyebrow), its name and its one-line blurb. A page that is one of a hub's
 * tabs (D209) is titled by its own tab, with the hub's name as the eyebrow and
 * the hub's tab bar under the title: Evals reads "Runs / Evals", not "Runs"
 * (GUIDE1, F914; Naveen: Evals and Usage "are great features but not visible").
 * A tab named as its hub (Apps › Apps) keeps the group. The words come from
 * `lib/nav.ts`, so a rename lands in the rail and the page together.
 *
 * No "use client": the route pages are server components, and this renders no
 * hooks.
 */
import { PageFrame, type PageWidth } from '@/components/ui';
import { hubOf } from '@/lib/nav';
import { AppHubTabs } from './AppHubTabs';

export function AppScreen({
    id, actions, bleed, width, eyebrow, title, description, children,
}: {
    /** The page's nav id (`orchestrations`, `logs`…). */
    id: string;
    actions?: React.ReactNode;
    /**
     * Words of the page's own in place of its nav entry's, for a page that
     * speaks to the reader (Home: today's date, a greeting, how things stand).
     * The menu keeps the entry's name.
     */
    eyebrow?: React.ReactNode;
    title?: React.ReactNode;
    description?: React.ReactNode;
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
            eyebrow={eyebrow ?? (tabbed && entry.label !== hub.label ? hub.label : group ?? 'Overview')}
            title={title ?? entry.label}
            description={description ?? entry.blurb}
            tabs={tabbed ? <AppHubTabs hubId={hub.id} current={entry.href} /> : undefined}
            actions={actions}
            bleed={bleed}
            width={width}
        >
            {children}
        </PageFrame>
    );
}
