import { RunsScreen } from '@/components/runs/RunsScreen';
import { Screen } from '@/components/ui';
import { navEntryFor } from '@/lib/nav';

export default function RunsPage() {
    const nav = navEntryFor('runs')!;

    return (
        <Screen title={nav.label} description={nav.blurb} width="wide">
            <RunsScreen />
        </Screen>
    );
}
