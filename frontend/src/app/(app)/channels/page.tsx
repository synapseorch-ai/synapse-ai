import { MessagingTab } from '@/components/settings/MessagingTab';

/*
 * Channels: the messaging bots, moved out of Settings into the Channels hub
 * (D209, M1a); /settings/messaging redirects here. The screen draws its own
 * frame (M2h-2): the list beside the open channel, on the wide frame.
 */
export default function ChannelsPage() {
    return <MessagingTab />;
}
