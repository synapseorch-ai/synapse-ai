import { RecordingPage } from '@/components/app/RecordingPage';

export const dynamic = 'force-dynamic';

/** A recording's own page (BRW2, F1142; Naveen 10-29). */
export default async function RecordingRoute(props: { params: Promise<{ id: string }> }) {
    const { id } = await props.params;
    return <RecordingPage id={id} />;
}
