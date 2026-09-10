import { OrchestrationTab } from '@/components/settings/OrchestrationTab';
import { Screen } from '@/components/ui';
import { navEntryFor } from '@/lib/nav';

export const dynamic = 'force-dynamic';

export default async function OrchestrationsPage(props: {
    searchParams: Promise<{ [key: string]: string | string[] | undefined }>;
}) {
    // Read `?run=` and `?id=` on the server and pass them down.
    // useSearchParams() would force this route's Suspense boundary client-side
    // and ship an empty initial HTML shell — which is exactly what happened to
    // /login.
    const searchParams = await props.searchParams;
    const runId = typeof searchParams.run === 'string' ? searchParams.run : undefined;
    const orchId = typeof searchParams.id === 'string' ? searchParams.id : undefined;
    const nav = navEntryFor('orchestrations')!;


    return (
        <Screen title={nav.label} description={nav.blurb} bleed>
            <OrchestrationTab initialRunId={runId} initialOrchId={orchId} />
        </Screen>
    );
}
