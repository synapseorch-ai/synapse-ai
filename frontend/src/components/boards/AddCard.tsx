'use client';

/* A person's card: a task to do or a note to know, read first by the next run on the board (C3, BOARD1b). */
import { useState } from 'react';
import { Plus } from 'lucide-react';

import { Button, Field, Input, Segmented, Textarea } from '@/components/ui';

import { PERSON_CARD_HINT, type CardDraft } from './board';

export function AddCard({ onAdd, busy, only, compact = false }: {
    onAdd: (draft: CardDraft) => Promise<boolean>;
    busy?: boolean;
    /** A column's own add: that kind only, no type picker ("Add a task" under Open, "Add a note" under Notes). */
    only?: 'task' | 'note';
    /** A column's foot: a quiet text button and no line of explanation under it. */
    compact?: boolean;
}) {
    const [open, setOpen] = useState(false);
    const [picked, setKind] = useState<'task' | 'note'>('task');
    const kind = only ?? picked;
    const [title, setTitle] = useState('');
    const [body, setBody] = useState('');

    const label = only === 'note' ? 'Add a note' : only === 'task' ? 'Add a task' : 'Add a card';
    if (!open) {
        return compact ? (
            <button type="button" onClick={() => setOpen(true)}
                className="flex w-full items-center gap-1.5 rounded-md px-2 py-1.5 text-xs font-medium text-text-muted hover:bg-surface hover:text-text focus-visible:outline-2 focus-visible:outline-accent">
                <Plus className="size-3.5" aria-hidden /> {label}
            </button>
        ) : (
            <div className="space-y-1">
                <Button size="sm" variant="secondary" onClick={() => setOpen(true)}>
                    <Plus className="size-3.5" aria-hidden /> {label}
                </Button>
                <p className="text-xs text-text-faint">{PERSON_CARD_HINT}</p>
            </div>
        );
    }
    const submit = async () => {
        if (await onAdd({ title: title.trim(), body: body.trim(), kind })) {
            setTitle(''); setBody(''); setOpen(false);
        }
    };
    return (
        <form className="space-y-3 rounded-md border border-person/40 bg-surface p-3 shadow-sm"
            onSubmit={e => { e.preventDefault(); if (title.trim()) submit(); }}>
            {!only && <Segmented label="Card type" value={kind} onChange={setKind} options={[
                { value: 'task', label: 'Task', hint: 'Work to do: an agent claims it and closes it with a result.' },
                { value: 'note', label: 'Note', hint: 'Something to know: never claimed, closed when no longer true.' },
            ]} />}
            <Field label="Title"><Input autoFocus value={title} onChange={e => setTitle(e.target.value)}
                placeholder={kind === 'task' ? 'Skip Harbor Test Supplies in every review' : 'Fenwick lead times change on 1 Nov'} /></Field>
            <Field label="Text (optional)"><Textarea rows={3} value={body} onChange={e => setBody(e.target.value)} /></Field>
            <p className="text-xs text-person-ink">{PERSON_CARD_HINT}</p>
            <div className="flex gap-2">
                <Button size="sm" type="submit" disabled={busy || !title.trim()}>Add</Button>
                <Button size="sm" variant="ghost" type="button" onClick={() => setOpen(false)}>Cancel</Button>
            </div>
        </form>
    );
}
