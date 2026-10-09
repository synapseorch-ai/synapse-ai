import { OrchestrationTab } from '@/components/settings/OrchestrationTab';

export const dynamic = 'force-dynamic';

export default async function WorkflowsPage(props: {
    searchParams: Promise<{ [key: string]: string | string[] | undefined }>;
}) {
    // Read `?run=` and `?id=` on the server and pass them down.
    // useSearchParams() would force this route's Suspense boundary client-side
    // and ship an empty initial HTML shell — which is exactly what happened to
    // /login.
    const searchParams = await props.searchParams;
    const runId = typeof searchParams.run === 'string' ? searchParams.run : undefined;
    const orchId = typeof searchParams.id === 'string' ? searchParams.id : undefined;
    // `?new=<n>` (⌘K's New workflow): a blank workflow on the canvas, once per value.
    const newRequest = typeof searchParams.new === 'string' ? searchParams.new : undefined;

    // The page draws its own frame (M2k): the list on the wide frame with its
    // actions in the header, an open workflow bleeding to the edges.
    return <OrchestrationTab initialRunId={runId} initialOrchId={orchId} newRequest={newRequest} />;
}
