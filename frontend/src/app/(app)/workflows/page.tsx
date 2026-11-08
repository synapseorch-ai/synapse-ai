import { WorkflowTabs } from '@/components/workflows/WorkflowTabs';

export const dynamic = 'force-dynamic';

export default async function WorkflowsPage(props: {
    searchParams: Promise<{ [key: string]: string | string[] | undefined }>;
}) {
    // `?settings=1` with `?id=`: the workflow opens with its settings (a run's "Raise the cap", CAP1). What the
    // address opens — `?id=`, `?run=`, ⌘K's `?new=<n>` — the tabs read live, so it still works once the page is
    // open (this page is dynamic, so that read is in the server's HTML too).
    const searchParams = await props.searchParams;
    const orchId = typeof searchParams.id === 'string' ? searchParams.id : undefined;
    const openSettings = searchParams.settings === '1';

    // The page draws its own frame (M2k): the list on the wide frame with its
    // actions in the header, an open workflow bleeding to the edges. Workflows
    // and their runs open as tabs (TABS1 v2).
    return <WorkflowTabs initialOrchId={orchId} openSettings={openSettings} />;
}
