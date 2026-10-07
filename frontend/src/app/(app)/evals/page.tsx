import { EvalsScreen } from '@/components/settings/EvalsScreen';
import { AppScreen } from '@/components/app/AppScreen';

export const dynamic = 'force-dynamic';

export default function EvalsPage() {
    return (
        <AppScreen id="evals">
            <EvalsScreen />
        </AppScreen>
    );
}
