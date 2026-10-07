import { TemplatesScreen } from '@/components/templates/TemplatesScreen';
import { Screen } from '@/components/ui';
import { navEntryFor } from '@/lib/nav';

export default function TemplatesPage() {
    const nav = navEntryFor('templates')!;

    return (
        <Screen title={nav.label} description={nav.blurb}>
            <TemplatesScreen />
        </Screen>
    );
}
