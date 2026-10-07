import { MessagingTab } from '@/components/settings/MessagingTab';
import { Screen } from '@/components/ui';
import { navEntryFor } from '@/lib/nav';

/*
 * Channels: the messaging bots, moved out of Settings into the Channels hub
 * (D209, M1a). The screen is Settings' Messaging tab, whole; /settings/messaging
 * redirects here.
 */
export default function ChannelsPage() {
    const nav = navEntryFor('messaging')!;

    return (
        <Screen title={nav.label} description={nav.blurb}>
            <MessagingTab />
        </Screen>
    );
}
