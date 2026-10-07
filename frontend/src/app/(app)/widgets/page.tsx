import { WidgetsTab } from '@/components/settings/WidgetsTab';
import { AppScreen } from '@/components/app/AppScreen';

/*
 * Widgets: an agent on someone's website, moved out of Settings into the
 * Channels hub (D209, M1a). The screen is Settings' Widgets tab, whole;
 * /settings/widgets redirects here.
 */
export default function WidgetsPage() {
    return (
        <AppScreen id="widgets">
            <WidgetsTab />
        </AppScreen>
    );
}
