import { SkillsScreen } from '@/components/settings/SkillsScreen';
import { Screen } from '@/components/ui';
import { navEntryFor } from '@/lib/nav';

export const dynamic = 'force-dynamic';

export default function SkillsPage() {
    const nav = navEntryFor('skills')!;

    return (
        <Screen title={nav.label} description={nav.blurb}>
            <SkillsScreen />
        </Screen>
    );
}
