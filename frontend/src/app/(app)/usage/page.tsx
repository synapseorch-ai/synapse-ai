import { UsageTab } from '@/components/settings/UsageTab';
import { AppScreen } from '@/components/app/AppScreen';

export default function UsagePage() {
    // On the wide frame like every page (M2b); the frame scrolls, so the
    // screen's actions sit beside its hero figure, not in a sticky row (M2g).
    return (
        <AppScreen id="usage" width="wide">
            <UsageTab />
        </AppScreen>
    );
}
