'use client';

/*
 * One board, as the hub, a workflow's canvas card and a schedule's drawer show it (C7, artboards 4A/4B; BOARD1b).
 *
 * A real board (Naveen at BOARD1b's screens): stickies in four columns, Notes · Open · Claimed · Done. A note is
 * something to know; a task moves left to right as agents claim and close it. A person drags a sticky where the
 * rules allow (`dropAction`): a task on Done asks for its result, a closed one goes back to Open; Claimed is the
 * agents' column. Every move is the same versioned write as an opened card's buttons, kept in its history and
 * undoable. On a workflow's board "This run" fades the earlier runs' stickies rather than moving them (C1a).
 * In a narrow place (a canvas card, a drawer) the columns stack. A person who may only read can neither drag nor see
 * a control that could be refused.
 */
import { useCallback, useEffect, useMemo, useState } from 'react';
import { Pencil, Trash2 } from 'lucide-react';

import {
    Button, ConfirmDialog, Drawer, ErrorNote, Field, Input, Modal, SearchInput, Segmented, Spinner, Textarea,
    Toast, useToast,
} from '@/components/ui';
import { cn } from '@/lib/cn';
import { count } from '@/lib/copy';

import { AccessList } from './AccessList';
import { AddCard } from './AddCard';
import { CardPanel, Sticky } from './Card';
import {
    COLUMN_TITLE, PERSON_CARD_HINT, boardsApi, columnsOf, dayAndTime, dropAction,
    type BoardPage, type BoardRef, type Card, type CardDraft, type Column, type CurrentRun,
} from './board';
import { useBoardsHost } from './host';

const RUN_WORDS: Record<string, string> = {
    running: 'is running', queued: 'is queued', paused: 'is waiting for a person', completed: 'finished',
    failed: 'failed', cancelled: 'was stopped',
};

const COLUMNS: Column[] = ['notes', 'open', 'claimed', 'done'];

/** What each column says while empty, and while a sticky that may not go there is dragged over it. */
const COLUMN_EMPTY: Record<Column, string> = {
    notes: 'Nothing to know yet.',
    open: 'No open tasks.',
    claimed: 'Agents claim tasks here.',
    done: 'Nothing closed yet.',
};

/** "this run started 14 Oct 09:00 and is running" */
export function runLine(run: CurrentRun | null | undefined): string {
    if (!run) return '';
    const words = RUN_WORDS[String(run.status || '')];
    return `this run started ${dayAndTime(run.started_at)}${words ? ` and ${words}` : ''}`;
}

export function BoardView({ boardRef, layout = 'page', onDeleted, onChanged }: {
    boardRef: BoardRef;
    /** `page`: the hub's board, with its name, actions and access list. `panel`: in place (a canvas card, a drawer). */
    layout?: 'page' | 'panel';
    onDeleted?: () => void;
    /** A card or the board changed: the list's counts are read again. */
    onChanged?: () => void;
}) {
    const host = useBoardsHost();
    const api = useMemo(() => boardsApi(host.request), [host.request]);
    const { canEdit } = host;
    const { toast, show, dismiss } = useToast();
    const [page, setPage] = useState<BoardPage | null>(null);
    const [error, setError] = useState('');
    const [thisRunOnly, setThisRunOnly] = useState(false);
    const [q, setQ] = useState('');
    const [openCard, setOpenCard] = useState<string | null>(null);
    const [busy, setBusy] = useState(false);
    const [editing, setEditing] = useState(false);
    const [deleting, setDeleting] = useState(false);
    const [name, setName] = useState('');
    const [description, setDescription] = useState('');
    const [now, setNow] = useState(() => Date.now());
    const [dragging, setDragging] = useState<Card | null>(null);
    const [over, setOver] = useState<Column | null>(null);
    const [finishing, setFinishing] = useState<Card | null>(null);
    const [outcome, setOutcome] = useState<'done' | 'failed'>('done');
    const [result, setResult] = useState('');
    const refKey = 'id' in boardRef ? boardRef.id : `${boardRef.scope}:${boardRef.scopeId}`;

    const load = useCallback(async () => {
        try {
            setPage(await api.open(boardRef, { q }));
            setNow(Date.now());
            setError('');
        } catch (e) {
            setError(e instanceof Error ? e.message : String(e));
        }
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [api, refKey, q]);
    useEffect(() => {
        const t = window.setTimeout(load, q ? 250 : 0);
        return () => window.clearTimeout(t);
    }, [load, q]);
    // Claims lapse and agents write: read again every half minute while the board is open.
    useEffect(() => {
        const t = window.setInterval(load, 30_000);
        return () => window.clearInterval(t);
    }, [load]);

    const changed = () => { load(); onChanged?.(); };
    const fail = (e: unknown) => show(e instanceof Error ? e.message : String(e), 'danger');
    const add = async (draft: CardDraft): Promise<boolean> => {
        setBusy(true);
        try {
            const { added } = await api.addCard(boardRef, draft);
            show(added ? 'Added. The next run reads it first.' : 'A card with that key is already on the board.');
            changed();
            return true;
        } catch (e) {
            fail(e);
            return false;
        } finally {
            setBusy(false);
        }
    };
    const move = async (card: Card, change: { status: 'open' | 'done' | 'failed'; result?: string }, said: string) => {
        setBusy(true);
        try {
            await api.change(card.id, { version: card.version, ...change });
            show(said);
            changed();
        } catch (e) {
            fail(e);
            load();
        } finally {
            setBusy(false);
        }
    };
    const drop = (column: Column) => {
        const card = dragging;
        setDragging(null);
        setOver(null);
        if (!card) return;
        const action = dropAction(card, column);
        if (action === 'finish') {
            setOutcome('done');
            setResult('');
            setFinishing(card);
        } else if (action === 'close') {
            move(card, { status: 'done' }, `Closed “${card.title}”: no longer true. Undo it from its history.`);
        } else if (action === 'reopen') {
            move(card, { status: 'open' }, `Reopened “${card.title}”.`);
        }
    };

    if (!page) {
        return error ? <ErrorNote>{error}</ErrorNote> : <div className="flex justify-center py-10"><Spinner /></div>;
    }
    const { board } = page;
    const runId = page.current_run?.run_id ?? null;
    const isWorkflow = board.scope === 'workflow';
    const shared = board.scope === 'shared';
    const columns = columnsOf(page);
    const empty = !page.cards.length && !page.done.length && !page.archived.length;
    const opened = page.cards.concat(page.done, page.archived).find(c => c.id === openCard) ?? null;

    const panel = (inline: boolean) => openCard && (
        <CardPanel cardId={openCard} api={api} canEdit={canEdit} currentRunId={runId} showBack={inline}
            onBack={() => setOpenCard(null)} onChanged={changed} />
    );
    // In place, an opened card takes the board's room; in the hub it opens in a drawer beside the board.
    if (layout === 'panel' && openCard) return <>{panel(true)}</>;

    return (
        <div className="space-y-5">
            {layout === 'page' ? (
                <header className="flex flex-wrap items-start gap-3">
                    <div className="min-w-0 flex-1">
                        <h3 className="truncate text-lg font-semibold tracking-[-0.01em] text-text">
                            {board.name}
                        </h3>
                        <p className="text-sm text-text-muted">
                            {shared ? (board.description || 'A shared board: only what its list names can use it.')
                                : isWorkflow ? `Kept across runs${runId ? ` · ${runLine(page.current_run)}` : ''}. Only this workflow's agents see it.`
                                    : "This schedule's agent reads it at every firing. Only that agent sees it."}
                        </p>
                    </div>
                    {canEdit && (shared || board.owner_gone) && (
                        <div className="flex gap-2">
                            {shared && (
                                <Button size="sm" variant="ghost" onClick={() => { setName(board.name); setDescription(board.description); setEditing(true); }}>
                                    <Pencil className="size-3.5" aria-hidden /> Rename
                                </Button>
                            )}
                            <Button size="sm" variant="ghost" onClick={() => setDeleting(true)}>
                                <Trash2 className="size-3.5" aria-hidden /> Delete
                            </Button>
                        </div>
                    )}
                </header>
            ) : (
                <p className="text-xs text-text-faint">
                    {isWorkflow ? `Kept across runs. Only this workflow's agents see it${runId ? `; ${runLine(page.current_run)}` : ''}.`
                        : "Kept across firings. Only this schedule's agent sees it."}
                </p>
            )}

            <div className="flex flex-wrap items-center gap-2">
                {isWorkflow && runId && (
                    <Segmented label="Which runs" value={thisRunOnly ? 'this' : 'all'} onChange={v => setThisRunOnly(v === 'this')}
                        options={[{ value: 'all', label: 'All runs' }, { value: 'this', label: 'This run' }]} />
                )}
                <SearchInput value={q} onChange={setQ} placeholder="Search cards, archived too" aria-label="Search the board's cards" />
            </div>

            {error && <ErrorNote>{error}</ErrorNote>}
            {q && empty && <p className="text-sm text-text-muted">No card matches.</p>}
            {!q && empty && (
                <p className="text-sm text-text-muted">
                    No cards yet. {isWorkflow ? "This workflow's agents add them as they work, and read them on the next run."
                        : shared ? 'Agents on its list add and work cards here; so can you.'
                            : 'The schedule adds cards as it works, and its next firing reads them first.'}
                </p>
            )}

            <div className="@container">
                <div className={cn('grid gap-3', layout === 'page' ? 'items-stretch @xl:grid-cols-2 @3xl:grid-cols-4' : 'items-start @xl:grid-cols-2')}>
                    {COLUMNS.map(column => {
                        const cards = columns[column];
                        const allowed = dragging ? dropAction(dragging, column) !== null : false;
                        const refused = dragging !== null && !allowed;
                        return (
                            <section
                                key={column}
                                aria-labelledby={`col-${refKey}-${column}`}
                                onDragOver={e => { if (allowed) { e.preventDefault(); e.dataTransfer.dropEffect = 'move'; setOver(column); } }}
                                onDragLeave={() => setOver(o => (o === column ? null : o))}
                                onDrop={e => { e.preventDefault(); if (allowed) drop(column); }}
                                className={cn(
                                    'flex min-h-24 flex-col gap-2 rounded-lg bg-surface-2 p-2 transition-colors',
                                    layout === 'page' && '@3xl:min-h-[28rem]',
                                    over === column && 'ring-2 ring-accent',
                                    refused && 'opacity-60',
                                )}
                            >
                                <h4 id={`col-${refKey}-${column}`} className="flex items-center justify-between px-1.5 pt-0.5 text-2xs font-semibold uppercase tracking-wider text-text-faint">
                                    <span>{COLUMN_TITLE[column]}</span>
                                    <span className="font-mono tabular-nums">{cards.length}</span>
                                </h4>
                                <ul className="flex flex-col gap-2">
                                    {cards.map(card => (
                                        <Sticky key={card.id} card={card} currentRunId={runId} now={now}
                                            dim={thisRunOnly && !!runId && card.run_id !== runId}
                                            draggable={canEdit && !busy && card.status !== 'archived'}
                                            onOpen={c => setOpenCard(c.id)} />
                                    ))}
                                </ul>
                                {!cards.length && (
                                    <p className="px-1.5 pb-1 text-xs text-text-faint">{COLUMN_EMPTY[column]}</p>
                                )}
                                {canEdit && !board.owner_gone && column === 'open' && <AddCard only="task" compact onAdd={add} busy={busy} />}
                                {canEdit && !board.owner_gone && column === 'notes' && <AddCard only="note" compact onAdd={add} busy={busy} />}
                            </section>
                        );
                    })}
                </div>
                {canEdit && !board.owner_gone && (
                    <p className="mt-2 text-xs text-text-faint">
                        {PERSON_CARD_HINT} Drag a task to Done when it is finished, or back to Open.
                    </p>
                )}
            </div>

            {shared && layout === 'page' && board.id && (
                <AccessList entries={page.access} canEdit={canEdit} onSave={async entries => {
                    await api.setAccess(board.id as string, entries);
                    show('Saved. Agents on the list get the board from their next turn.');
                    changed();
                }} />
            )}

            {layout === 'page' && (
                <Drawer open={openCard !== null} onClose={() => setOpenCard(null)} width="lg"
                    title={opened ? (opened.kind === 'note' ? 'Note' : 'Task') : 'Card'}>
                    {panel(false)}
                </Drawer>
            )}

            <Modal open={finishing !== null} onClose={() => setFinishing(null)} size="sm"
                title={finishing ? `Close “${finishing.title}”` : 'Close the task'}
                description="The result is kept on the card, for the next run and for anyone who opens it." footer={
                    <>
                        <Button variant="ghost" onClick={() => setFinishing(null)}>Cancel</Button>
                        <Button disabled={busy || (outcome === 'failed' && !result.trim())} onClick={async () => {
                            const card = finishing;
                            setFinishing(null);
                            if (card) {
                                await move(card, { status: outcome, result: result.trim() || undefined },
                                    outcome === 'failed' ? `Marked “${card.title}” failed.` : `Marked “${card.title}” done.`);
                            }
                        }}>{outcome === 'failed' ? 'Mark failed' : 'Mark done'}</Button>
                    </>
                }>
                <div className="space-y-3">
                    <Segmented label="How it ended" value={outcome} onChange={setOutcome}
                        options={[{ value: 'done', label: 'Done' }, { value: 'failed', label: 'Failed' }]} />
                    <Field label={outcome === 'failed' ? 'Why it failed' : 'Result (optional)'}>
                        <Textarea rows={3} value={result} onChange={e => setResult(e.target.value)} autoFocus />
                    </Field>
                </div>
            </Modal>

            <Modal open={editing} onClose={() => setEditing(false)} title="Rename the board" size="sm" footer={
                <>
                    <Button variant="ghost" onClick={() => setEditing(false)}>Cancel</Button>
                    <Button disabled={busy || !name.trim()} onClick={async () => {
                        setBusy(true);
                        try {
                            await api.rename(board.id as string, { name: name.trim(), description });
                            setEditing(false);
                            changed();
                        } catch (e) {
                            fail(e);
                        } finally {
                            setBusy(false);
                        }
                    }}>Save</Button>
                </>
            }>
                <div className="space-y-3">
                    <Field label="Name"><Input value={name} onChange={e => setName(e.target.value)} /></Field>
                    <Field label="Description"><Textarea rows={3} value={description} onChange={e => setDescription(e.target.value)} /></Field>
                </div>
            </Modal>
            <ConfirmDialog open={deleting} onClose={() => setDeleting(false)} busy={busy}
                title={`Delete ${board.name}?`}
                description={`Its ${count(board.open + board.claimed, 'open card')} and every card's history go with it. Agents on its list stop seeing it.`}
                onConfirm={async () => {
                    setBusy(true);
                    try {
                        await api.remove(board.id as string);
                        setDeleting(false);
                        onDeleted?.();
                    } catch (e) {
                        fail(e);
                    } finally {
                        setBusy(false);
                    }
                }} />
            <Toast message={toast.message} tone={toast.tone} onDismiss={dismiss} />
            {/* Dragging ends however it ends (dropped outside, Esc): no column stays lit. */}
            {dragging && <DragEnd onEnd={() => { setDragging(null); setOver(null); }} />}
            <DragStart onStart={id => setDragging(page.cards.concat(page.done).find(c => c.id === id) ?? null)} />
        </div>
    );
}

/** Which sticky a drag picked up, read from the document's dragstart (the sticky sets its id on the transfer). */
function DragStart({ onStart }: { onStart: (cardId: string) => void }) {
    useEffect(() => {
        const listen = (e: DragEvent) => {
            const el = (e.target as HTMLElement | null)?.closest?.('[data-board-card]');
            const id = el?.getAttribute('data-board-card');
            if (id) onStart(id);
        };
        document.addEventListener('dragstart', listen);
        return () => document.removeEventListener('dragstart', listen);
    }, [onStart]);
    return null;
}

function DragEnd({ onEnd }: { onEnd: () => void }) {
    useEffect(() => {
        document.addEventListener('dragend', onEnd);
        return () => document.removeEventListener('dragend', onEnd);
    }, [onEnd]);
    return null;
}
