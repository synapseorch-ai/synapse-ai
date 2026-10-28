'use client';
/*
 * A recording's own page in OSS (BRW2, F1142; Naveen 10-29: its own page): the synced kit's editor in the app's page
 * frame, its title the recording's name and its eyebrow the way back to Recordings.
 */
import Link from 'next/link';
import { useRouter } from 'next/navigation';

import { RecordingEditor, type EditorFrame } from '@/components/browser';
import { PageFrame } from '@/components/ui';
import { OSS_BROWSER_HOST } from '@/lib/browserHost';
import { OSS_RECORDINGS_HOST } from '@/lib/recordingsHost';
import { OSS_SESSIONS_HOST } from '@/lib/sessionsHost';

const frame: EditorFrame = ({ title, description, actions, back, children }) => (
    <PageFrame
        breadcrumb={<Link href={back.href} className="underline-offset-2 hover:underline">{back.label}</Link>}
        title={title}
        description={description}
        actions={actions}
    >
        {children}
    </PageFrame>
);

export function RecordingPage({ id }: { id: string }) {
    const router = useRouter();
    return (
        <RecordingEditor host={OSS_RECORDINGS_HOST} sessionsHost={OSS_SESSIONS_HOST} browserHost={OSS_BROWSER_HOST}
                         id={id} frame={frame} onDeleted={() => router.push(OSS_RECORDINGS_HOST.libraryHref)} />
    );
}
