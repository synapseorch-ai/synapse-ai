import { ToolsScreen } from '@/components/settings/ToolsScreen';

export const dynamic = 'force-dynamic';

/*
 * A real page. It used to render <SettingsView initialTab="custom_tools" />, which
 * meant this screen's state was held in a 1,087-line component alongside the
 * Models tab's forty API-key fields and drilled back down as props.
 *
 * The frame and the title both come from the nav entry (`AppScreen`), so there
 * is still one place the words live. The tools screen draws it: Import OpenAPI
 * and New Tool sit in the header and answer to its unsaved-edit question (M2f).
 */
export default function ToolsPage() {
    return <ToolsScreen />;
}
