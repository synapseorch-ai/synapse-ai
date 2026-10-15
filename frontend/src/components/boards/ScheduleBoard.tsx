'use client';

/*
 * A scheduled agent's board, in its schedule's drawer (C7; BOARD1b): the cards its next firing reads first. A
 * schedule that runs a workflow has none of its own: its cards are on that workflow's board.
 *
 * Folded until opened, so a schedule reads as simple as before (Naveen, 2026-10-16: optional, not to scare anyone).
 * The folded head says how many cards wait, since a person's card there is an instruction the next run reads.
 */
import { useEffect, useMemo, useState } from 'react';

import { Section } from '@/components/ui';

import { boardsApi } from './board';
import { BoardView } from './BoardView';
import { useBoardsHost } from './host';

export function ScheduleBoard({ scheduleId, workflowId }: {
    scheduleId: string;
    /** The workflow the schedule runs, when it runs one. */
    workflowId?: string | null;
}) {
    const host = useBoardsHost();
    const api = useMemo(() => boardsApi(host.request), [host.request]);
    const [waiting, setWaiting] = useState(0);
    const [reads, setReads] = useState(0);

    useEffect(() => {
        if (workflowId) return;
        let live = true;
        api.open({ scope: 'schedule', scopeId: scheduleId })
            .then(page => { if (live) setWaiting(page.cards.length); })
            .catch(() => { if (live) setWaiting(0); });
        return () => { live = false; };
    }, [api, scheduleId, workflowId, reads]);

    if (workflowId) {
        const href = host.linkTo?.('workflow', workflowId);
        return (
            <Section key={scheduleId} variant="plain" title="Board · optional"
                description="This schedule runs a workflow, so its cards are on the workflow's board.">
                <p className="text-sm text-text-muted">
                    {href ? <><a className="text-text underline-offset-2 hover:underline" href={href}>Open the workflow</a>, then Board.</>
                        : <>Open the workflow, then Board.</>}
                </p>
            </Section>
        );
    }
    return (
        <Section key={scheduleId} variant="plain" title="Board · optional"
            description="Notes and tasks its agent reads first at each run."
            summary={waiting ? `${waiting} open` : undefined}>
            <BoardView boardRef={{ scope: 'schedule', scopeId: scheduleId }} layout="panel"
                onChanged={() => setReads(n => n + 1)} />
        </Section>
    );
}
