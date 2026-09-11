import { EvalsScreen } from '@/components/settings/EvalsScreen';
import { Screen } from '@/components/ui';
import { navEntryFor } from '@/lib/nav';

export const dynamic = 'force-dynamic';

export default function EvalsPage() {
    const nav = navEntryFor('evals')!;

    return (
        <Screen title={nav.label} description={nav.blurb}>
            <EvalsScreen />
        </Screen>
    );
}
