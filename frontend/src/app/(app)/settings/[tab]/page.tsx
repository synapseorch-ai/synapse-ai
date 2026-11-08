import { redirect } from 'next/navigation';

import { SettingsView } from '@/components/SettingsView';
import { isSettingsEntry, navEntryFor } from '@/lib/nav';

export const dynamic = 'force-dynamic';

export default async function SettingsPage(props: {
    params: Promise<{ tab: string }>;
    searchParams: Promise<{ [key: string]: string | string[] | undefined }>;
}) {
    const { tab } = await props.params;
    const searchParams = await props.searchParams;

    // The moved URLs are redirected at the routing layer by next.config.ts, so
    // they never reach this component. A rail page's id (`/settings/evals`)
    // used to pass here and render Settings with that page's title over an
    // empty body (M0-base, defect 8): it goes to its own page now. Anything
    // else that isn't a settings tab is a typo — the settings landing page.
    if (!isSettingsEntry(tab)) redirect(navEntryFor(tab)?.href ?? '/settings/general');

    const subTab = typeof searchParams.tab === 'string' ? searchParams.tab : undefined;
    return <SettingsView initialTab={tab} initialSubTab={subTab} />;
}
