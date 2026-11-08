'use client';
/*
 * Agents › Browser in OSS (BRW2, F1139; DESIGN v5-10): the synced kit's Sessions library and, from F1142,
 * Recordings, each a tab with its own address so a picker's "Manage sessions" lands on its tab.
 */
import { useRouter } from 'next/navigation';

import { RecordingsLibrary, SessionsLibrary } from '@/components/browser';
import { HubTabs } from '@/components/ui';
import { OSS_BROWSER_HOST } from '@/lib/browserHost';
import { OSS_RECORDINGS_HOST } from '@/lib/recordingsHost';
import { OSS_SESSIONS_HOST } from '@/lib/sessionsHost';

const TABS = [
    { href: '/browser/sessions', label: 'Sessions' },
    { href: '/browser/recordings', label: 'Recordings' },
];

export function BrowserLibrary({ tab }: { tab: 'sessions' | 'recordings' }) {
    const router = useRouter();
    return (
        // A gap, not space-y: HubTabs' own -mb-px outranks space-y's margin, which left the intro touching the tabs.
        <div className="flex flex-col gap-6">
            <HubTabs label="Browser" tabs={TABS} current={`/browser/${tab}`} />
            {tab === 'sessions' ? (
                <SessionsLibrary host={OSS_SESSIONS_HOST} browserHost={OSS_BROWSER_HOST} />
            ) : (
                <RecordingsLibrary host={OSS_RECORDINGS_HOST} sessionsHost={OSS_SESSIONS_HOST} browserHost={OSS_BROWSER_HOST}
                                   onOpen={id => router.push(OSS_RECORDINGS_HOST.recordingHref(id))} />
            )}
        </div>
    );
}
