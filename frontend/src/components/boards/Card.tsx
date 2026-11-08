'use client';

/*
 * A card in a board's list, and a card opened: its text, its state, its comments and its history, where a person
 * can take back any change (C4). A person's card is amber — an instruction the next run reads first — and a card
 * written after outside content says so (BOARD1b, D282).
 */
import { useCallback, useEffect, useState } from 'react';
import { ArrowLeft, CheckCircle2, RotateCcw, SquareCheck, StickyNote, XCircle } from 'lucide-react';

import { Badge, Button, ErrorNote, Field, Hint, Input, Segmented, Spinner, Textarea, tileHue } from '@/components/ui';
import { when } from '@/components/run/model';
import { cn } from '@/lib/cn';
import { VaultText } from './VaultText';

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

/** Who a card is with, as a round tile: the agent holding it, else the one that wrote it; a person in amber. */
export function Avatar({ name, person = false, size = 'sm' }: { name: string; person?: boolean; size?: 'sm' | 'md' }) {
    const letters = name.trim().split(/\s+/).slice(0, 2).map(w => w[0]?.toUpperCase() ?? '').join('') || '?';
    const hue = tileHue(name.toLowerCase());
    return (
        <span
            title={name}
            aria-label={name}
            className={cn('inline-flex shrink-0 items-center justify-center rounded-full font-semibold',
                size === 'sm' ? 'size-6 text-[0.625rem]' : 'size-7 text-xs',
                person && 'bg-person-subtle text-person-ink ring-1 ring-person/40')}
            style={person ? undefined : {
                backgroundColor: `oklch(var(--tile-bg-l) var(--tile-bg-c) ${hue})`,
                color: `oklch(var(--tile-fg-l) var(--tile-fg-c) ${hue})`,
            }}
        >
            {letters}
        </span>
    );
}

/** Whose a card is now: the holder of a claim, else its writer. */
export function cardOwner(card: Card): { name: string; person: boolean } {
    if (card.status === 'claimed') return { name: holderName(card), person: false };
    return { name: actorName(card.created_by), person: card.by_person };
}

/**
 * One card on the board, in Jira's manner (Naveen at BOARD1b's screens: its look, not its features — agents claim
 * and close the work): a compact white card showing its title; a footer with its kind, its key where an agent gave
 * one, its marks, and the avatar of whoever has it. Colour only for meaning: a person's card has an amber edge and
 * avatar, a claim a cyan dot and time left, a closed one a green tick or a red cross. It opens on a click and, where a
 * person may move it, drags between columns.
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
    const owner = cardOwner(card);
    return (
        <li className={cn('list-none transition-opacity', dim && 'opacity-40 hover:opacity-100')}>
            <button
                type="button"
                draggable={draggable}
                onDragStart={draggable ? e => { e.dataTransfer.setData('text/x-board-card', card.id); e.dataTransfer.effectAllowed = 'move'; } : undefined}
                onClick={() => onOpen(card)}
                data-board-card={card.id}
                title={provenance(card, currentRunId)}
                className={cn(
                    'block w-full rounded-md border border-border bg-surface px-3 pb-2 pt-2.5 text-left',
                    'shadow-[0_1px_1px_rgb(0_0_0/0.08)] transition-colors hover:bg-surface-2 focus-visible:outline-2 focus-visible:outline-accent',
                    card.by_person && 'border-l-[3px] border-l-person',
                    draggable && 'cursor-grab active:cursor-grabbing',
                )}
            >
                <span className={cn('line-clamp-3 block text-sm leading-snug text-text', closed && 'text-text-muted')}>{card.title}</span>
                <span className="mt-2 flex min-w-0 items-center gap-1.5 text-xs">
                    {note
                        ? <StickyNote className="size-3.5 shrink-0 text-text-faint" aria-label="Note" />
                        : closed
                            ? (card.outcome === 'failed'
                                ? <XCircle className="size-3.5 shrink-0 text-danger" aria-label="Failed" />
                                : <CheckCircle2 className="size-3.5 shrink-0 text-success" aria-label="Done" />)
                            : <SquareCheck className="size-3.5 shrink-0 text-accent" aria-label="Task" />}
                    <span className={cn('min-w-0 truncate font-medium', owner.person ? 'text-person-ink' : 'text-text-muted')}>{owner.name}</span>
                </span>
                <span className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1">
                    {card.created_at && <span className="text-2xs text-text-faint">{when(card.created_at, { now })}</span>}
                    {card.status === 'claimed' && (
                        <span className="inline-flex shrink-0 items-center gap-1 text-2xs text-text-muted">
                            <span aria-hidden className="size-1.5 rounded-full bg-machine" />{left || 'claimed'}
                        </span>
                    )}
                    {card.tainted && <FromContent plain />}
                    {card.status === 'archived' && <Badge size="sm" tone="neutral">Archived</Badge>}
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
    const [activity, setActivity] = useState<'history' | 'comments'>('history');

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

    const owner = cardOwner(card);
    const writer = actorName(card.created_by);
    const fields: [string, React.ReactNode][] = [
        ['Status', <Badge key="s" size="sm" tone={card.status === 'claimed' ? 'machine' : card.outcome === 'failed' ? 'danger' : 'neutral'}>{statusLabel(card)}</Badge>],
        [card.status === 'claimed' ? 'Held by' : 'With', <span key="o" className="flex items-center gap-2"><Avatar name={owner.name} person={owner.person} />{owner.name}</span>],
        ['Written by', <span key="w" className={cn('flex items-center gap-2', card.by_person && 'text-person-ink')}><Avatar name={writer} person={card.by_person} />{writer}</span>],
        ['Kind', task ? 'Task' : 'Note'],
        ...(card.key ? [['Key', <span key="k" className="font-mono text-xs">{card.key}</span>] as [string, React.ReactNode]] : []),
        ...(card.run_id ? [['Run', card.run_id === currentRunId ? 'This run' : card.run_id] as [string, React.ReactNode]] : []),
        ...(card.status === 'claimed' ? [['Claim', heldFor(card, Date.now()) || 'ending'] as [string, React.ReactNode]] : []),
        ['Created', dayAndTime(card.created_at)],
        ...(card.updated_at ? [['Updated', dayAndTime(card.updated_at)] as [string, React.ReactNode]] : []),
        ...(card.closed_at ? [['Closed', dayAndTime(card.closed_at)] as [string, React.ReactNode]] : []),
        ...(card.tainted ? [['Content', <FromContent key="c" />] as [string, React.ReactNode]] : []),
    ];

    return (
        <div className="@container space-y-4">
            {showBack && <BackLink onBack={onBack} />}
            <div className="grid gap-6 @xl:grid-cols-[minmax(0,1fr)_14rem]">
                <div className="min-w-0 space-y-5">
                    {editing ? (
                        <div className="space-y-3">
                            <Field label="Title"><Input value={title} onChange={e => setTitle(e.target.value)} /></Field>
                            <Field label="Description"><Textarea rows={5} value={body} onChange={e => setBody(e.target.value)} /></Field>
                            <div className="flex gap-2">
                                <Button size="sm" disabled={busy || !title.trim()} onClick={() => act(
                                    () => api.change(card.id, { version: card.version, title: title.trim(), body }),
                                    () => setEditing(false))}>Save</Button>
                                <Button size="sm" variant="ghost" onClick={() => setEditing(false)}>Cancel</Button>
                            </div>
                        </div>
                    ) : (
                        <header className="space-y-3">
                            <h3 className="text-xl font-semibold leading-snug tracking-[-0.01em] text-text">{card.title}</h3>
                            <section className="space-y-1">
                                <h4 className="text-xs font-semibold text-text-muted">Description</h4>
                                {card.body
                                    ? <p className="whitespace-pre-wrap text-sm leading-relaxed text-text"><VaultText text={card.body} /></p>
                                    : <p className="text-sm text-text-faint">None.</p>}
                            </section>
                        </header>
                    )}

                    {card.result && (
                        <section className="space-y-1">
                            <h4 className="text-xs font-semibold text-text-muted">{card.outcome === 'failed' ? 'Why it failed' : 'Result'}</h4>
                            <p className="whitespace-pre-wrap text-sm text-text"><VaultText text={card.result} /></p>
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

                    <section className="space-y-3" aria-label="Activity">
                        <div className="flex items-center gap-3">
                            <h4 className="text-xs font-semibold text-text-muted">Activity</h4>
                            <Segmented label="Activity" value={activity} onChange={setActivity}
                                options={[{ value: 'history', label: 'History' }, { value: 'comments', label: `Comments · ${page.comments.length}` }]} />
                        </div>
                        {activity === 'comments' ? (
                            <div className="space-y-3">
                                {page.comments.length === 0 && <p className="text-xs text-text-faint">No comments.</p>}
                                <ul className="space-y-3">
                                    {page.comments.map(c => (
                                        <li key={c.seq} className="flex gap-2.5 text-sm">
                                            <Avatar name={actorName(c.by)} person={!!c.by.person} />
                                            <span className="min-w-0">
                                                <span className={cn('text-xs font-medium', c.by.person ? 'text-person-ink' : 'text-text')}>{actorName(c.by)}</span>
                                                <span className="text-xs text-text-faint"> · {dayAndTime(c.created_at)}</span>
                                                <p className="whitespace-pre-wrap text-text">{c.comment}</p>
                                            </span>
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
                            </div>
                        ) : (
                            <div className="space-y-2">
                                <ol className="space-y-1">
                                    {page.history.map(e => (
                                        <li key={e.seq} className="flex min-h-9 items-center gap-2.5 text-sm">
                                            <Avatar name={actorName(e.by)} person={!!e.by?.person} />
                                            <span className="min-w-0 flex-1">
                                                <span className={cn('block truncate', e.by?.person ? 'text-person-ink' : 'text-text')}>{eventLine(e)}</span>
                                                <span className="block text-xs text-text-faint">
                                                    <span className="font-mono tabular-nums">#{e.seq}</span> · {dayAndTime(e.created_at)}{e.tainted ? ' · after outside content' : ''}
                                                </span>
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
                                <p className="text-xs text-text-faint">Any change can be taken back for 30 days.</p>
                            </div>
                        )}
                    </section>
                </div>

                <aside aria-label="Details" className="h-fit rounded-md border border-border p-3">
                    <dl className="grid grid-cols-[5rem_minmax(0,1fr)] items-center gap-x-2.5 gap-y-3 text-sm">
                        {fields.map(([label, value]) => (
                            <div key={label} className="contents">
                                <dt className="text-xs text-text-muted">{label}</dt>
                                <dd className="min-w-0 break-words text-text">{value}</dd>
                            </div>
                        ))}
                    </dl>
                </aside>
            </div>
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
