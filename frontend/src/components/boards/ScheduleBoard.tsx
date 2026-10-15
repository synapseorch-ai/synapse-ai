'use client';

/*
 * A scheduled agent's board, in its schedule's drawer (C7; BOARD1b): the cards its next firing reads first. A
 * schedule that runs a workflow has none of its own: its cards are on that workflow's board.
 */
import { BoardView } from './BoardView';
import { useBoardsHost } from './host';

export function ScheduleBoard({ scheduleId, workflowId }: {
    scheduleId: string;
    /** The workflow the schedule runs, when it runs one. */
    workflowId?: string | null;
}) {
    const { linkTo } = useBoardsHost();
    if (workflowId) {
        const href = linkTo?.('workflow', workflowId);
        return (
            <p className="text-sm text-text-muted">
                This schedule runs a workflow, so its cards are on the workflow&apos;s board
                {href ? <> (<a className="text-text underline-offset-2 hover:underline" href={href}>open the workflow</a>, then Board)</> : null}.
            </p>
        );
    }
    return <BoardView boardRef={{ scope: 'schedule', scopeId: scheduleId }} layout="panel" />;
}
