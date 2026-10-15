'use client';

/*
 * One board, as the hub, a workflow's canvas card and a schedule's drawer show it (C7, artboards 4A/4B; BOARD1b).
 *
 * A workflow's board lasts across runs: this run's cards come first, and "This run" shows them alone (C1a). A card
 * opens in place, with its history and Undo. A shared board also shows who may use it (C8). A person who may only
 * read sees no control that could be refused.
 */
import { useCallback, useEffect, useMemo, useState } from 'react';
import { Pencil, Trash2 } from 'lucide-react';

import {
    Button, ConfirmDialog, EmptyState, ErrorNote, Field, Input, Modal, SearchInput, Segmented, Spinner, Textarea,
    Toast, useToast,
} from '@/components/ui';
import { count } from '@/lib/copy';

import { AccessList } from './AccessList';
import { AddCard } from './AddCard';
import { CardPanel, CardRow } from './Card';
import {
    boardsApi, dayAndTime, splitByRun, splitByStatus,
    type BoardPage, type BoardRef, type Card, type CardDraft, type CurrentRun,
} from './board';
import { useBoardsHost } from './host';

const RUN_WORDS: Record<string, string> = {
    running: 'is running', queued: 'is queued', paused: 'is waiting for a person', completed: 'finished',
    failed: 'failed', cancelled: 'was stopped',
};

/** "this run started 14 Oct 09:00 and is running" */
export function runLine(run: CurrentRun | null | undefined): string {
    if (!run) return '';
    const words = RUN_WORDS[String(run.status || '')];
    return `this run started ${dayAndTime(run.started_at)}${words ? ` and ${words}` : ''}`;
}

function Group({ title, cards, currentRunId, now, onOpen }: {
    title: string; cards: Card[]; currentRunId?: string | null; now: number; onOpen: (c: Card) => void;
}) {
    if (!cards.length) return null;
    return (
        <section className="space-y-1" aria-label={title}>
            <h4 className="px-2.5 text-2xs font-semibold uppercase tracking-wider text-text-faint">{title}</h4>
            <ul className="space-y-0.5">
                {cards.map(c => <CardRow key={c.id} card={c} currentRunId={currentRunId} now={now} onOpen={onOpen} />)}
            </ul>
        </section>
    );
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
    const [run, setRun] = useState<'all' | 'this'>('all');
    const [q, setQ] = useState('');
    const [openCard, setOpenCard] = useState<string | null>(null);
    const [busy, setBusy] = useState(false);
    const [editing, setEditing] = useState(false);
    const [deleting, setDeleting] = useState(false);
    const [name, setName] = useState('');
    const [description, setDescription] = useState('');
    const [now, setNow] = useState(() => Date.now());
    const refKey = 'id' in boardRef ? boardRef.id : `${boardRef.scope}:${boardRef.scopeId}`;

    const load = useCallback(async () => {
        try {
            setPage(await api.open(boardRef, { run, q }));
            setNow(Date.now());
            setError('');
        } catch (e) {
            setError(e instanceof Error ? e.message : String(e));
        }
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [api, refKey, run, q]);
    useEffect(() => { setOpenCard(null); }, [refKey]);
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
    const add = async (draft: CardDraft): Promise<boolean> => {
        setBusy(true);
        try {
            const { added } = await api.addCard(boardRef, draft);
            show(added ? 'Card added. The next run reads it first.' : 'A card with that key is already on the board.');
            changed();
            return true;
        } catch (e) {
            show(e instanceof Error ? e.message : String(e), 'danger');
            return false;
        } finally {
            setBusy(false);
        }
    };

    if (!page) {
        return error ? <ErrorNote>{error}</ErrorNote> : <div className="flex justify-center py-10"><Spinner /></div>;
    }
    const { board } = page;
    const runId = page.current_run?.run_id ?? null;
    const isWorkflow = board.scope === 'workflow';
    const shared = board.scope === 'shared';
    const empty = !page.cards.length && !page.done.length && !page.archived.length;

    if (openCard) {
        return (
            <CardPanel cardId={openCard} api={api} canEdit={canEdit} currentRunId={runId}
                onBack={() => setOpenCard(null)} onChanged={changed} />
        );
    }

    const byRun = splitByRun(page.cards, runId);
    const byStatus = splitByStatus(page.cards);
    const open = (c: Card) => setOpenCard(c.id);
    const groups = isWorkflow && runId && (layout === 'panel' || run === 'all')
        ? [
            { title: `This run · ${dayAndTime(page.current_run?.started_at)}`, cards: byRun.thisRun },
            { title: 'Earlier runs', cards: byRun.earlier },
        ]
        : [
            { title: `Open · ${byStatus.open.length}`, cards: byStatus.open },
            { title: `Claimed · ${byStatus.claimed.length}`, cards: byStatus.claimed },
        ];

    return (
        <div className="space-y-5">
            {layout === 'page' && (
                <header className="flex flex-wrap items-start gap-3">
                    <div className="min-w-0 flex-1">
                        <h3 className="truncate text-lg font-semibold tracking-[-0.01em] text-text">
                            {board.owner_gone ? `${board.name} (deleted ${board.scope})` : board.name}
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
            )}
            {layout === 'panel' && (
                <p className="text-xs text-text-faint">
                    {isWorkflow ? `Kept across runs. Only this workflow's agents see it${runId ? `; ${runLine(page.current_run)}` : ''}.`
                        : "Kept across firings. Only this schedule's agent sees it."}
                </p>
            )}

            <div className="flex flex-wrap items-center gap-2">
                {isWorkflow && runId && layout === 'page' && (
                    <Segmented label="Which runs" value={run} onChange={setRun}
                        options={[{ value: 'all', label: 'All runs' }, { value: 'this', label: 'This run' }]} />
                )}
                <SearchInput value={q} onChange={setQ} placeholder="Search cards, archived too" aria-label="Search the board's cards" />
            </div>

            {error && <ErrorNote>{error}</ErrorNote>}
            {empty && !q ? (
                <EmptyState title="No cards yet">
                    {isWorkflow ? "This workflow's agents add cards as they work, and read them on the next run."
                        : shared ? 'Agents on its list add and work cards here; so can you.'
                            : 'The schedule adds cards as it works, and its next firing reads them first.'}
                </EmptyState>
            ) : empty ? (
                <p className="text-sm text-text-muted">No card matches.</p>
            ) : (
                <div className="space-y-4">
                    {groups.map(g => <Group key={g.title} {...g} currentRunId={runId} now={now} onOpen={open} />)}
                    <Group title={`Done · ${page.done.length}`} cards={page.done} currentRunId={runId} now={now} onOpen={open} />
                    <Group title={`Archived · ${page.archived.length}`} cards={page.archived} currentRunId={runId} now={now} onOpen={open} />
                </div>
            )}

            {canEdit && !board.owner_gone && <AddCard onAdd={add} busy={busy} />}

            {shared && layout === 'page' && board.id && (
                <AccessList entries={page.access} canEdit={canEdit} onSave={async entries => {
                    await api.setAccess(board.id as string, entries);
                    show('Saved. Agents on the list get the board from their next turn.');
                    changed();
                }} />
            )}

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
                            show(e instanceof Error ? e.message : String(e), 'danger');
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
                        show(e instanceof Error ? e.message : String(e), 'danger');
                    } finally {
                        setBusy(false);
                    }
                }} />
            <Toast message={toast.message} tone={toast.tone} onDismiss={dismiss} />
        </div>
    );
}
