import { AgentsScreen } from '@/components/settings/AgentsScreen';
import { AppScreen } from '@/components/app/AppScreen';

export const dynamic = 'force-dynamic';

/*
 * A real page. It used to render <SettingsView initialTab="agents" />, which
 * meant this screen's state was held in a 1,087-line component alongside the
 * Models tab's forty API-key fields and drilled back down as props.
 *
 * The frame and the title both come from the nav entry (`AppScreen`), as
 * they did when SettingsView wrapped this — so there is still one place the
 * words live.
 */
export default function AgentsPage() {
    return (
        <AppScreen id="agents">
            <AgentsScreen />
        </AppScreen>
    );
}
