import { WorkflowTabs } from '@/components/workflows/WorkflowTabs';

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
    // `?settings=1` with `?id=`: the workflow opens with its settings (a run's "Raise the cap", CAP1).
    const openSettings = searchParams.settings === '1';

    // The page draws its own frame (M2k): the list on the wide frame with its
    // actions in the header, an open workflow bleeding to the edges. Workflows
    // and their runs open as tabs (TABS1 v2).
    return <WorkflowTabs initialRunId={runId} initialOrchId={orchId} newRequest={newRequest} openSettings={openSettings} />;
}
