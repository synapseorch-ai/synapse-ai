import { UsageTab } from '@/components/settings/UsageTab';
import { AppScreen } from '@/components/app/AppScreen';

export default function UsagePage() {
    // UsageTab scrolls itself and keeps a sticky action row, so it takes the
    // flex column rather than an outer scroll container.

    return (
        <AppScreen id="usage" bleed>
            <UsageTab />
        </AppScreen>
    );
}
