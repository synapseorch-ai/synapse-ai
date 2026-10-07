/*
 * A rail page's frame, from its nav entry (M1a).
 *
 * Every page under a hub says the same three things in the same place: the
 * menu group it sits in (the eyebrow), its name, and its one-line blurb. They
 * come from `lib/nav.ts`, so a rename lands in the rail and the page together.
 *
 * No "use client": the route pages are server components, and this renders no
 * hooks.
 */
import { PageFrame, type PageWidth } from '@/components/ui';
import { hubOf } from '@/lib/nav';

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
    const { group, entry } = found;
    return (
        <PageFrame
            eyebrow={group ?? 'Overview'}
            title={entry.label}
            description={entry.blurb}
            actions={actions}
            bleed={bleed}
            width={width}
        >
            {children}
        </PageFrame>
    );
}
