import { RunsScreen } from '@/components/runs/RunsScreen';
import { AppScreen } from '@/components/app/AppScreen';

export default function RunsPage() {
    return (
        <AppScreen id="runs" width="wide">
            <RunsScreen />
        </AppScreen>
    );
}
