'use client';

/*
 * A card in a board's list, and a card opened: its text, its state, its comments and its history, where a person
 * can take back any change (C4). A person's card is amber — an instruction the next run reads first — and a card
 * written after outside content says so (BOARD1b, D282).
 */
import { useCallback, useEffect, useState } from 'react';
import { ArrowLeft, CheckCircle2, CircleDot, RotateCcw, StickyNote, XCircle } from 'lucide-react';

import { Badge, Button, ErrorNote, Field, Hint, Input, Spinner, Textarea } from '@/components/ui';
import { cn } from '@/lib/cn';

import {
    FROM_CONTENT_HINT, actorName, dayAndTime, eventLine, heldFor, holderName, provenance, statusLabel,
    type BoardsApi, type Card, type CardPage,
} from './board';

export function FromContent() {
    return (
        <Hint content={FROM_CONTENT_HINT}>
            <span tabIndex={0} className="inline-flex rounded-sm focus-visible:outline-2 focus-visible:outline-accent">
                <Badge size="sm" tone="neutral">From content</Badge>
            </span>
        </Hint>
    );
}

/** One card in a list: a 36px-and-up row that opens it. */
export function CardRow({ card, currentRunId, now, onOpen }: {
    card: Card;
    currentRunId?: string | null;
    now: number;
    onOpen: (card: Card) => void;
}) {
    const held = heldFor(card, now);
    const closed = card.status === 'done' || card.status === 'archived';
    return (
        <li>
            <button
                type="button"
                onClick={() => onOpen(card)}
                className={cn(
                    'group flex w-full min-h-9 items-start gap-2.5 rounded-md border border-transparent px-2.5 py-2 text-left',
                    'hover:border-border hover:bg-surface-2 focus-visible:outline-2 focus-visible:outline-accent',
                    card.by_person && 'border-l-2 border-l-person pl-2',
                )}
            >
                <span className="mt-0.5 shrink-0 text-text-faint" aria-hidden>
                    {card.kind === 'note' ? <StickyNote className="size-3.5" />
                        : closed ? (card.outcome === 'failed' ? <XCircle className="size-3.5 text-danger" /> : <CheckCircle2 className="size-3.5 text-success" />)
                            : <CircleDot className={cn('size-3.5', card.status === 'claimed' && 'text-machine')} />}
                </span>
                <span className="min-w-0 flex-1">
                    <span className={cn('block truncate text-sm font-medium text-text', closed && 'text-text-muted')}>{card.title}</span>
                    {card.body && <span className="block truncate text-xs text-text-muted">{card.body}</span>}
                    <span className={cn('mt-0.5 block truncate text-xs', card.by_person ? 'text-person-ink' : 'text-text-faint')}>
                        {card.status === 'claimed' ? `Claimed by ${holderName(card)}${held ? ` · ${held}` : ''}` : provenance(card, currentRunId)}
                    </span>
                </span>
                <span className="flex shrink-0 items-center gap-1.5">
                    {card.tainted && <FromContent />}
                    {card.kind === 'note' && <Badge size="sm" tone="neutral">Note</Badge>}
                    {closed && <Badge size="sm" tone={card.outcome === 'failed' ? 'danger' : 'neutral'}>{statusLabel(card)}</Badge>}
                </span>
            </button>
        </li>
    );
}

/** A card opened: read it, change it against the version read, close or reopen it, comment, and undo. */
export function CardPanel({ cardId, api, canEdit, currentRunId, onBack, onChanged }: {
    cardId: string;
    api: BoardsApi;
    canEdit: boolean;
    currentRunId?: string | null;
    onBack: () => void;
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
                <BackLink onBack={onBack} />
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
            <BackLink onBack={onBack} />
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
