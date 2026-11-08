'use client';

/*
 * Home's four figures as one strip (M2a; UX1's Home): what is running, what
 * waits on the reader, what failed this week and how much of the week
 * completed. Colour only as a dot (UX1: four tiles in four hues outshouted the
 * brand); every number in the text colour, so a 0% never reads as fine.
 */
import type { ReactNode } from 'react';

import { StatStrip } from '@/components/ui';

import { rate } from './home';
import { type Clock } from './model';
import { Took, When } from './When';

export function HomeStats({
    running, nextRun, waiting, waitingSince, failed, failedSub, completed, runs, capped = false, clock,
}: {
    running: number;
    /** The soonest scheduled run, for "Next: …". */
    nextRun?: string | null;
    /** Runs and tool calls waiting on the reader. */
    waiting: number;
    /** When the oldest of them started waiting. */
    waitingSince?: string | null;
    failed: number;
    /** Under the failures: the newest one's workflow, or what else to know (the dead-letter queue). */
    failedSub?: ReactNode;
    completed: number;
    runs: number;
    /** The week was counted over a capped list (OSS: the latest 200 runs). */
    capped?: boolean;
    clock: Clock;
}) {
    return (
        <StatStrip
            label="This week"
            cells={[
                {
                    label: 'Running now', dot: 'machine', value: running,
                    sub: nextRun ? <>Next: <When iso={nextRun} clock={clock} /></> : 'Nothing scheduled',
                },
                {
                    label: 'Waiting on you', dot: 'person', value: waiting,
                    sub: waiting ? (waitingSince ? <>for <Took start={waitingSince} end={null} /></> : 'waiting') : 'all clear',
                },
                {
                    label: 'Failed · 7 days', dot: 'danger', value: failed,
                    sub: failedSub ?? (failed ? undefined : 'none'),
                },
                {
                    label: 'Completed · 7 days', dot: 'success', value: rate(completed, runs),
                    sub: runs ? `${completed} of ${capped ? 'the latest ' : ''}${runs} ${runs === 1 ? 'run' : 'runs'}` : 'No runs yet',
                },
            ]}
        />
    );
}
