import { WidgetsTab } from '@/components/settings/WidgetsTab';

/*
 * Widgets: an agent on someone's website, moved out of Settings into the
 * Channels hub (D209, M1a). /settings/widgets redirects here. The screen draws
 * its own frame (M2h-2): the list beside the open widget, on the wide frame.
 */
export default function WidgetsPage() {
    return <WidgetsTab />;
}
