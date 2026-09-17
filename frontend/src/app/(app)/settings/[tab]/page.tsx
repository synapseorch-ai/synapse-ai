import { redirect } from 'next/navigation';

import { SettingsView } from '@/components/SettingsView';
import { navEntryFor } from '@/lib/nav';

export const dynamic = 'force-dynamic';

export default async function SettingsPage(props: {
    params: Promise<{ tab: string }>;
    searchParams: Promise<{ [key: string]: string | string[] | undefined }>;
}) {
    const { tab } = await props.params;
    const searchParams = await props.searchParams;

    // The eight moved URLs are redirected at the routing layer by
    // next.config.ts, so they never reach this component. Anything else that
    // isn't a real tab is a typo — send it to the settings landing page.
    if (!navEntryFor(tab)) redirect('/settings/general');

    const subTab = typeof searchParams.tab === 'string' ? searchParams.tab : undefined;
    return <SettingsView initialTab={tab} initialSubTab={subTab} />;
}
