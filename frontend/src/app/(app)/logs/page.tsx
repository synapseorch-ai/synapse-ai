import { LogsTab } from '@/components/settings/LogsTab';
import { AppScreen } from '@/components/app/AppScreen';

export default function LogsPage() {
    return (
        <AppScreen id="logs" bleed>
            <LogsTab />
        </AppScreen>
    );
}
