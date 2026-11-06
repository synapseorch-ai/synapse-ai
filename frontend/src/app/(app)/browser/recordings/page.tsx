import { AppScreen } from '@/components/app/AppScreen';
import { BrowserLibrary } from '@/components/app/BrowserLibrary';

export const dynamic = 'force-dynamic';

/** Agents › Browser › Recordings (BRW2, F1142): its own folder holds each recording's page, so the tab is named here. */
export default function BrowserRecordingsPage() {
    return (
        <AppScreen id="browser" width="wide">
            <BrowserLibrary tab="recordings" />
        </AppScreen>
    );
}
