import { McpServersScreen } from '@/components/settings/McpServersScreen';
import { Screen } from '@/components/ui';
import { navEntryFor } from '@/lib/nav';

export const dynamic = 'force-dynamic';

/*
 * A real page. It used to render <SettingsView initialTab="mcp_servers" />, which
 * meant this screen's state was held in a 1,087-line component alongside the
 * Models tab's forty API-key fields and drilled back down as props.
 *
 * The <Screen> shell and the title both come from the nav entry, exactly as
 * they did when SettingsView wrapped this — so the header is unchanged and
 * there is still one place the words live.
 */
export default function AppsPage() {
    const nav = navEntryFor('mcp_servers')!;

    return (
        <Screen title={nav.label} description={nav.blurb}>
            <McpServersScreen />
        </Screen>
    );
}
