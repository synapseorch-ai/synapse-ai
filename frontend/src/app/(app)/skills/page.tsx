import { SkillsScreen } from '@/components/settings/SkillsScreen';
import { AppScreen } from '@/components/app/AppScreen';

export const dynamic = 'force-dynamic';

export default function SkillsPage() {
    return (
        <AppScreen id="skills">
            <SkillsScreen />
        </AppScreen>
    );
}
