import { redirect } from 'next/navigation';

import { AppScreen } from '@/components/app/AppScreen';
import { BrowserLibrary } from '@/components/app/BrowserLibrary';

export const dynamic = 'force-dynamic';

const TABS = ['sessions', 'recordings'] as const;

/** Agents › Browser, on one of its two tabs (BRW2, F1139; DESIGN v5-10): Sessions, Recordings. */
export default async function BrowserTabPage(props: { params: Promise<{ tab: string }> }) {
    const { tab } = await props.params;
    if (!(TABS as readonly string[]).includes(tab)) redirect('/browser/sessions');
    return (
        <AppScreen id="browser" width="wide">
            <BrowserLibrary tab={tab as (typeof TABS)[number]} />
        </AppScreen>
    );
}
