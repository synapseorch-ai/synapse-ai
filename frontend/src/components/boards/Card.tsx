'use client';

/*
 * A card in a board's list, and a card opened: its text, its state, its comments and its history, where a person
 * can take back any change (C4). A person's card is amber — an instruction the next run reads first — and a card
 * written after outside content says so (BOARD1b, D282).
 */
import { useCallback, useEffect, useState } from 'react';
import { ArrowLeft, CheckCircle2, RotateCcw, XCircle } from 'lucide-react';

import { Badge, Button, ErrorNote, Field, Hint, Input, Spinner, Textarea } from '@/components/ui';
import { cn } from '@/lib/cn';

import {
    FROM_CONTENT_HINT, actorName, dayAndTime, eventLine, heldFor, holderName, provenance, statusLabel, timeLeft,
    type BoardsApi, type Card, type CardPage,
} from './board';

export function FromContent({ plain = false }: { plain?: boolean } = {}) {
    // Inside a sticky (itself a button) a focusable hint would be a control inside a control: the words ride `title`.
    if (plain) return <span title={FROM_CONTENT_HINT}><Badge size="sm" tone="neutral">From content</Badge></span>;
    return (
        <Hint content={FROM_CONTENT_HINT}>
            <span tabIndex={0} className="inline-flex rounded-sm focus-visible:outline-2 focus-visible:outline-accent">
                <Badge size="sm" tone="neutral">From content</Badge>
            </span>
        </Hint>
    );
}

/**
 * One card as a sticky (Naveen at BOARD1b's screens): a raised paper tile that opens the card, and, where a person
 * may move it, drags between columns. Colour only for meaning: a note is a folded-corner tile on a warmer paper; a
 * person's sticky has an amber strip across its top, like tape; a claimed one a cyan dot, its holder and time left.
 */
export function Sticky({ card, currentRunId, now, dim, draggable, onOpen }: {
    card: Card;
    currentRunId?: string | null;
    now: number;
    /** Another run's, while "This run" is picked: shown faded rather than moved. */
    dim?: boolean;
    draggable?: boolean;
    onOpen: (card: Card) => void;
}) {
    const note = card.kind === 'note';
    const closed = card.status === 'done' || card.status === 'archived';
    const left = timeLeft(card, now);
    return (
        <li className={cn('list-none transition-opacity', dim && 'opacity-40 hover:opacity-100')}>
            <button
                type="button"
                draggable={draggable}
                onDragStart={draggable ? e => { e.dataTransfer.setData('text/x-board-card', card.id); e.dataTransfer.effectAllowed = 'move'; } : undefined}
                onClick={() => onOpen(card)}
                data-board-card={card.id}
                className={cn(
                    'relative block w-full overflow-hidden rounded-md border border-border px-3 pb-2.5 pt-3 text-left shadow-sm',
                    'transition-[box-shadow,transform] hover:-translate-y-px hover:shadow-md focus-visible:outline-2 focus-visible:outline-accent',
                    note ? 'bg-[color-mix(in_oklab,var(--color-surface)_93%,#b8a27a)]' : 'bg-surface',
                    draggable && 'cursor-grab active:cursor-grabbing',
                    closed && 'shadow-none',
                )}
            >
                {card.by_person && <span aria-hidden className="absolute inset-x-4 top-0 h-1.5 rounded-b-sm bg-person" />}
                {note && (
                    <span aria-hidden className="absolute right-0 top-0 size-3.5 bg-[linear-gradient(225deg,var(--color-surface-2)_50%,var(--color-border-strong)_50%)]" />
                )}
                <span className={cn('block text-sm font-medium leading-snug text-text', closed && 'text-text-muted line-through decoration-text-faint/60')}>
                    {closed && card.kind === 'task' && (card.outcome === 'failed'
                        ? <XCircle className="mr-1 inline size-3.5 align-[-2px] text-danger" aria-hidden />
                        : <CheckCircle2 className="mr-1 inline size-3.5 align-[-2px] text-success" aria-hidden />)}
                    {card.title}
                </span>
                {card.body && !closed && <span className="mt-1 line-clamp-3 block text-xs leading-relaxed text-text-muted">{card.body}</span>}
                {closed && card.result && <span className="mt-1 line-clamp-2 block text-xs text-text-muted">{card.result}</span>}
                <span className="mt-2 flex flex-wrap items-center gap-1.5">
                    {card.status === 'claimed' ? (
                        <span className="inline-flex items-center gap-1.5 text-xs text-text-muted">
                            <span aria-hidden className="size-1.5 rounded-full bg-machine" />
                            {holderName(card)}{left ? ` · ${left}` : ''}
                        </span>
                    ) : (
                        <span className={cn('text-xs', card.by_person ? 'text-person-ink' : 'text-text-faint')}>
                            {provenance(card, currentRunId)}
                        </span>
                    )}
                    {card.tainted && <FromContent plain />}
                    {card.status === 'archived' && <Badge size="sm" tone="neutral">Archived</Badge>}
                    {card.key && <span className="font-mono text-2xs text-text-faint">{card.key}</span>}
                </span>
            </button>
        </li>
    );
}

/** A card opened: read it, change it against the version read, close or reopen it, comment, and undo. */
export function CardPanel({ cardId, api, canEdit, currentRunId, onBack, onChanged, showBack = true }: {
    cardId: string;
    api: BoardsApi;
    canEdit: boolean;
    currentRunId?: string | null;
    onBack: () => void;
    /** "Back to the board" above the card: in place (a canvas card, a drawer); not in the hub's own drawer. */
    showBack?: boolean;
    /** Something on the card changed: the board reads its list again. */
    onChanged: () => void;
}) {
    const [page, setPage] = useState<CardPage | null>(null);
    const [error, setError] = useState('');
    const [busy, setBusy] = useState(false);
    const [editing, setEditing] = useState(false);
    const [title, setTitle] = useState('');
    const [body, setBody] = useState('');
    const [closing, setClosing] = useState<'done' | 'failed' | null>(null);
    const [result, setResult] = useState('');
    const [comment, setComment] = useState('');

    const load = useCallback(async () => {
        try {
            setPage(await api.card(cardId));
            setError('');
        } catch (e) {
            setError(e instanceof Error ? e.message : String(e));
        }
    }, [api, cardId]);
    useEffect(() => { load(); }, [load]);

    const act = async (what: () => Promise<unknown>, after?: () => void) => {
        setBusy(true);
        try {
            await what();
            setError('');
            after?.();
            onChanged();
        } catch (e) {
            setError(e instanceof Error ? e.message : String(e));
        } finally {
            setBusy(false);
            await load();
        }
    };

    if (!page) {
        return (
            <div className="space-y-3">
                {showBack && <BackLink onBack={onBack} />}
                {error ? <ErrorNote>{error}</ErrorNote> : <div className="flex justify-center py-8"><Spinner /></div>}
            </div>
        );
    }
    const { card } = page;
    const open = card.status === 'open' || card.status === 'claimed';
    const archived = card.status === 'archived';
    const task = card.kind === 'task';

    return (
        <div className="space-y-5">
            {showBack && <BackLink onBack={onBack} />}
            <header className="space-y-1.5">
                <div className="flex flex-wrap items-center gap-1.5">
                    <Badge size="sm" tone="neutral">{task ? 'Task' : 'Note'}</Badge>
                    <Badge size="sm" tone={card.status === 'claimed' ? 'machine' : card.outcome === 'failed' ? 'danger' : 'neutral'}>{statusLabel(card)}</Badge>
                    {card.by_person && <Badge size="sm" tone="person">From a person</Badge>}
                    {card.tainted && <FromContent />}
                    {card.key && <span className="font-mono text-2xs text-text-faint">{card.key}</span>}
                </div>
                {editing ? (
                    <div className="space-y-3">
                        <Field label="Title"><Input value={title} onChange={e => setTitle(e.target.value)} /></Field>
                        <Field label="Text"><Textarea rows={4} value={body} onChange={e => setBody(e.target.value)} /></Field>
                        <div className="flex gap-2">
                            <Button size="sm" disabled={busy || !title.trim()} onClick={() => act(
                                () => api.change(card.id, { version: card.version, title: title.trim(), body }),
                                () => setEditing(false))}>Save</Button>
                            <Button size="sm" variant="ghost" onClick={() => setEditing(false)}>Cancel</Button>
                        </div>
                    </div>
                ) : (
                    <>
                        <h3 className="text-[15px] font-semibold text-text">{card.title}</h3>
                        {card.body && <p className="whitespace-pre-wrap text-sm leading-relaxed text-text-muted">{card.body}</p>}
                    </>
                )}
                <p className={cn('text-xs', card.by_person ? 'text-person-ink' : 'text-text-faint')}>
                    {provenance(card, currentRunId)}
                    {card.status === 'claimed' && ` · claimed by ${holderName(card)}, ${heldFor(card, Date.now()) || 'its claim is ending'}`}
                </p>
            </header>

            {card.result && (
                <section className="space-y-1">
                    <h4 className="text-xs font-semibold uppercase tracking-wider text-text-faint">{card.outcome === 'failed' ? 'Why it failed' : 'Result'}</h4>
                    <p className="whitespace-pre-wrap text-sm text-text">{card.result}</p>
                </section>
            )}

            {error && <ErrorNote>{error}</ErrorNote>}

            {canEdit && !archived && !editing && (
                <div className="space-y-3">
                    <div className="flex flex-wrap gap-2">
                        <Button size="sm" variant="secondary" disabled={busy} onClick={() => {
                            setTitle(card.title); setBody(card.body); setEditing(true);
                        }}>Edit</Button>
                        {open && task && <Button size="sm" variant="secondary" disabled={busy} onClick={() => { setClosing('done'); setResult(''); }}>Mark done</Button>}
                        {open && task && <Button size="sm" variant="secondary" disabled={busy} onClick={() => { setClosing('failed'); setResult(''); }}>Mark failed</Button>}
                        {open && !task && <Button size="sm" variant="secondary" disabled={busy} onClick={() => act(
                            () => api.change(card.id, { version: card.version, status: 'done' }))}>No longer true</Button>}
                        {!open && <Button size="sm" variant="secondary" disabled={busy} onClick={() => act(
                            () => api.change(card.id, { version: card.version, status: 'open' }))}>Reopen</Button>}
                    </div>
                    {closing && (
                        <div className="space-y-2">
                            <Field label={closing === 'failed' ? 'Why it failed' : 'Result (optional)'}>
                                <Textarea rows={3} value={result} onChange={e => setResult(e.target.value)} />
                            </Field>
                            <div className="flex gap-2">
                                <Button size="sm" disabled={busy || (closing === 'failed' && !result.trim())} onClick={() => act(
                                    () => api.change(card.id, { version: card.version, status: closing, result: result.trim() || undefined }),
                                    () => setClosing(null))}>{closing === 'failed' ? 'Mark failed' : 'Mark done'}</Button>
                                <Button size="sm" variant="ghost" onClick={() => setClosing(null)}>Cancel</Button>
                            </div>
                        </div>
                    )}
                </div>
            )}

            <section className="space-y-2" aria-labelledby={`comments-${card.id}`}>
                <h4 id={`comments-${card.id}`} className="text-xs font-semibold uppercase tracking-wider text-text-faint">Comments</h4>
                {page.comments.length === 0 && <p className="text-xs text-text-faint">No comments.</p>}
                <ul className="space-y-2">
                    {page.comments.map(c => (
                        <li key={c.seq} className="text-sm">
                            <span className={cn('text-xs font-medium', c.by.person ? 'text-person-ink' : 'text-text-muted')}>{actorName(c.by)}</span>
                            <span className="text-xs text-text-faint"> · {dayAndTime(c.created_at)}</span>
                            <p className="whitespace-pre-wrap text-text">{c.comment}</p>
                        </li>
                    ))}
                </ul>
                {canEdit && !archived && (
                    <div className="flex gap-2">
                        <Input aria-label="Add a comment" placeholder="Add a comment" value={comment} onChange={e => setComment(e.target.value)} />
                        <Button size="sm" variant="secondary" disabled={busy || !comment.trim()} onClick={() => act(
                            () => api.comment(card.id, comment.trim()), () => setComment(''))}>Comment</Button>
                    </div>
                )}
            </section>

            <section className="space-y-2" aria-labelledby={`history-${card.id}`}>
                <h4 id={`history-${card.id}`} className="text-xs font-semibold uppercase tracking-wider text-text-faint">History</h4>
                <ol className="space-y-1">
                    {page.history.map(e => (
                        <li key={e.seq} className="flex min-h-9 items-center gap-2 text-sm">
                            <span className="w-6 shrink-0 font-mono text-2xs tabular-nums text-text-faint">{e.seq}</span>
                            <span className="min-w-0 flex-1">
                                <span className={cn('block truncate', e.by?.person ? 'text-person-ink' : 'text-text')}>{eventLine(e)}</span>
                                <span className="block text-xs text-text-faint">{dayAndTime(e.created_at)}{e.tainted ? ' · after outside content' : ''}</span>
                            </span>
                            {canEdit && e.undoable && (
                                <Button size="sm" variant="ghost" disabled={busy} onClick={() => act(() => api.undo(card.id, e.seq))}
                                    aria-label={`Undo change ${e.seq}: ${eventLine(e)}`}>
                                    <RotateCcw className="size-3.5" aria-hidden /> Undo
                                </Button>
                            )}
                        </li>
                    ))}
                </ol>
                <p className="text-xs text-text-faint">Changes can be taken back for 30 days.</p>
            </section>
        </div>
    );
}

function BackLink({ onBack }: { onBack: () => void }) {
    return (
        <button type="button" onClick={onBack}
            className="inline-flex items-center gap-1.5 rounded-sm text-xs font-medium text-text-muted hover:text-text focus-visible:outline-2 focus-visible:outline-accent">
            <ArrowLeft className="size-3.5" aria-hidden /> Back to the board
        </button>
    );
}
