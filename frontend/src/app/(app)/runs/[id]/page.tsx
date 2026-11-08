import { RunPage } from '@/components/runs/RunPage';

/** One run, as a path (M2b). The page reads the run in the browser; see `RunPage`. */
export default async function RunDetailPage({ params }: { params: Promise<{ id: string }> }) {
    const { id } = await params;
    return <RunPage runId={id} />;
}
