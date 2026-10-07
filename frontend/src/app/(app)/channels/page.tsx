import { MessagingTab } from '@/components/settings/MessagingTab';
import { AppScreen } from '@/components/app/AppScreen';

/*
 * Channels: the messaging bots, moved out of Settings into the Channels hub
 * (D209, M1a). The screen is Settings' Messaging tab, whole; /settings/messaging
 * redirects here.
 */
export default function ChannelsPage() {
    return (
        <AppScreen id="messaging">
            <MessagingTab />
        </AppScreen>
    );
}
