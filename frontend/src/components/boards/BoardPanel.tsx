'use client';

/*
 * A workflow's board in its canvas card (C7, artboard 4B; BOARD1b): the card's frame — a header with close — around
 * the board, this run first. Opened from the canvas top bar's "Board" button.
 */
import { ClipboardList, X } from 'lucide-react';

import { useHelpTopic } from '@/components/help';
import { IconButton } from '@/components/ui';

import { BoardView } from './BoardView';

export function BoardPanel({ workflowId, name, onClose }: { workflowId: string; name: string; onClose: () => void }) {
    useHelpTopic('orchestrations.board');
    return (
        <section aria-label="This workflow's board" className="flex h-full min-h-0 w-full flex-col">
            <div className="flex items-center gap-2.5 border-b border-border px-4 py-3">
                <span className="inline-flex size-7 shrink-0 items-center justify-center rounded-md bg-surface-2 text-text-muted" aria-hidden>
                    <ClipboardList className="size-4" />
                </span>
                <div className="min-w-0 flex-1">
                    <h3 className="truncate text-[15px] font-semibold tracking-[-0.01em] text-text">This workflow&apos;s board</h3>
                    <p className="truncate text-xs text-text-faint" translate="no">{name || 'Untitled workflow'}</p>
                </div>
                <IconButton label="Close the board" onClick={onClose} size="sm" icon={X} />
            </div>
            <div className="min-h-0 flex-1 overflow-y-auto p-4">
                <BoardView boardRef={{ scope: 'workflow', scopeId: workflowId }} layout="panel" />
            </div>
        </section>
    );
}
