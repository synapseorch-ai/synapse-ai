'use client';
/*
 * Agents › Browser in OSS (BRW2, F1139; DESIGN v5-10): the synced kit's Sessions library and, from F1142,
 * Recordings, each a tab with its own address so a picker's "Manage sessions" lands on its tab.
 */
import { SessionsLibrary } from '@/components/browser';
import { EmptyState, HubTabs } from '@/components/ui';
import { OSS_BROWSER_HOST } from '@/lib/browserHost';
import { OSS_SESSIONS_HOST } from '@/lib/sessionsHost';

const TABS = [
    { href: '/browser/sessions', label: 'Sessions' },
    { href: '/browser/recordings', label: 'Recordings' },
];

export function BrowserLibrary({ tab }: { tab: 'sessions' | 'recordings' }) {
    return (
        <div className="space-y-6">
            <HubTabs label="Browser" tabs={TABS} current={`/browser/${tab}`} />
            {tab === 'sessions' ? (
                <SessionsLibrary host={OSS_SESSIONS_HOST} browserHost={OSS_BROWSER_HOST} />
            ) : (
                <EmptyState title="No recordings yet.">
                    Do a task once in the browser while Synapse records it, and your agents and workflows can repeat it.
                </EmptyState>
            )}
        </div>
    );
}
