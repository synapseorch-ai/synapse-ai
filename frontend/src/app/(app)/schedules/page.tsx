import { SchedulesTab } from '@/components/settings/SchedulesTab';
import { AppScreen } from '@/components/app/AppScreen';

export default function SchedulesPage() {
    return (
        <AppScreen id="schedules" bleed>
            <SchedulesTab />
        </AppScreen>
    );
}
