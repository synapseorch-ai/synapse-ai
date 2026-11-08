'use client';

/**
 * The ways on from a refused save (F871): it stays open until the person picks one, unlike the toast that said the
 * 404 for four seconds. Changed elsewhere → reload theirs, or keep mine (a save over theirs, from the time named).
 * Deleted elsewhere → save mine as a new workflow, or discard my edits. The draft is never touched until they pick.
 */

import { Button, Modal } from '@/components/ui';

import { changedWhen, type SaveProblem } from '../saveConflict';

export function SaveConflictDialog({ problem, noun = 'workflow', busy, onReload, onKeepMine, onSaveAsNew, onDiscard, onClose }: {
    problem: SaveProblem | null;
    /** What was saved: a workflow (F871), an agent or a tool (F891). */
    noun?: 'workflow' | 'agent' | 'tool';
    busy?: boolean;
    onReload: () => void;
    onKeepMine: () => void;
    onSaveAsNew: () => void;
    onDiscard: () => void;
    /** Leaves the draft as it is, unsaved; the bar still says what happened. */
    onClose: () => void;
}) {
    const changed = problem?.kind === 'changed';
    return (
        <Modal
            open={problem !== null}
            onClose={onClose}
            title={changed ? `This ${noun} was changed elsewhere` : `This ${noun} was deleted elsewhere`}
            description={changed
                ? `Someone saved it ${changedWhen(problem.at)}, in another tab or another session. Your edits are still here.`
                : 'It was deleted in another tab or by someone else. Your edits are still here.'}
            size="sm"
            footer={changed ? (
                <>
                    <Button variant="secondary" onClick={onReload} disabled={busy}>Reload theirs</Button>
                    <Button onClick={onKeepMine} loading={busy}>Keep mine</Button>
                </>
            ) : (
                <>
                    <Button variant="secondary" onClick={onDiscard} disabled={busy}>Discard my edits</Button>
                    <Button onClick={onSaveAsNew} loading={busy}>Save as a new {noun}</Button>
                </>
            )}
        >
            <p className="text-sm text-text-muted">
                {changed
                    ? 'Reload theirs to see what changed (your edits go). Keep mine to save yours over theirs.'
                    : `Save as a new ${noun} to keep your edits, or discard them.`}
            </p>
        </Modal>
    );
}
