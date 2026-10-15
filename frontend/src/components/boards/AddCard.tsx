'use client';

/* A person's card: a task to do or a note to know, read first by the next run on the board (C3, BOARD1b). */
import { useState } from 'react';
import { Plus } from 'lucide-react';

import { Button, Field, Input, Segmented, Textarea } from '@/components/ui';

import { PERSON_CARD_HINT, type CardDraft } from './board';

export function AddCard({ onAdd, busy }: { onAdd: (draft: CardDraft) => Promise<boolean>; busy?: boolean }) {
    const [open, setOpen] = useState(false);
    const [kind, setKind] = useState<'task' | 'note'>('task');
    const [title, setTitle] = useState('');
    const [body, setBody] = useState('');

    if (!open) {
        return (
            <div className="space-y-1">
                <Button size="sm" variant="secondary" onClick={() => setOpen(true)}>
                    <Plus className="size-3.5" aria-hidden /> Add a card
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
        <form className="space-y-3 rounded-md border border-person/40 bg-person-subtle/40 p-3"
            onSubmit={e => { e.preventDefault(); if (title.trim()) submit(); }}>
            <Segmented label="Card type" value={kind} onChange={setKind} options={[
                { value: 'task', label: 'Task', hint: 'Work to do: an agent claims it and closes it with a result.' },
                { value: 'note', label: 'Note', hint: 'Something to know: never claimed, closed when no longer true.' },
            ]} />
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
