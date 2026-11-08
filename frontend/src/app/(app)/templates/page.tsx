import { TemplatesScreen } from '@/components/templates/TemplatesScreen';
import { AppScreen } from '@/components/app/AppScreen';

export default function TemplatesPage() {
    return (
        <AppScreen id="templates" width="wide">
            <TemplatesScreen />
        </AppScreen>
    );
}
