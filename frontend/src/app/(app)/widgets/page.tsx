import { WidgetsTab } from '@/components/settings/WidgetsTab';
import { Screen } from '@/components/ui';
import { navEntryFor } from '@/lib/nav';

/*
 * Widgets: an agent on someone's website, moved out of Settings into the
 * Channels hub (D209, M1a). The screen is Settings' Widgets tab, whole;
 * /settings/widgets redirects here.
 */
export default function WidgetsPage() {
    const nav = navEntryFor('widgets')!;

    return (
        <Screen title={nav.label} description={nav.blurb}>
            <WidgetsTab />
        </Screen>
    );
}
