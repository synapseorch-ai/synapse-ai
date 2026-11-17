'use client';

/**
 * The answer-field editor (F1618): rows to four levels, "Paste an example", "Paste a JSON Schema", and the schema the
 * fields compile to. A model step's Answer fields (`steps.tsx`) and a custom tool's "What it answers" (F1620) are this
 * one editor, each with its own words.
 */
import { useState, type ReactNode } from 'react';
import { Plus, Trash2 } from 'lucide-react';
import { Button, Checkbox, Select, Textarea } from '@/components/ui';
import { nextHumanFieldName } from '../humanFields';
import {
    ANSWER_FIELD_TYPES, LIST_ITEM_KINDS, MAX_LEVELS, STORED_ONLY_LABELS, answerFieldAs, answerFieldChildren, answerFieldKind,
    answerFieldNameOk, answerFieldOptions, answerFieldsFromExample, answerFieldsFromSchema, answerFieldWords, answerJsonSchema,
    withAnswerFieldChildren, type AnswerField, type PastedFields,
} from '../answerFields';
import { LocalInput } from './shared';

/** What the editor says, by where it is. */
export type AnswerFieldsWords = {
    /** The section's test id. */
    testId: string;
    /** Its heading, "(optional)" beside it; left out where the section around it heads it in its own style. */
    title?: string;
    intro: ReactNode;
    /** The button that adds a field at the first level. */
    add: string;
    /** The type a field added there starts as. */
    addType: AnswerField['type'];
    /** The read-only schema under the rows: its summary, its line, and its accessible name. */
    shown: { title: string; intro: string; label: string };
};

/** The editor: `fields` and what changes them, said in `words`. */
export function AnswerFieldsEditor({ fields, onChange, words }: {
    fields: AnswerField[]; onChange: (next: AnswerField[]) => void; words: AnswerFieldsWords;
}) {
    const [paste, setPaste] = useState<null | 'example' | 'schema'>(null);
    return (
        <div className="space-y-1.5" data-testid={words.testId}>
            {words.title && (
                <span className="block text-sm font-medium text-text">
                    {words.title} <span className="text-xs font-normal text-text-faint">(optional)</span>
                </span>
            )}
            <p className="text-[11px] text-text-faint">
                {words.intro}
            </p>
            <AnswerFieldRows fields={fields} onChange={onChange} level={1} path="" />
            <div className="flex flex-wrap gap-1.5">
                <Button
                    size="sm"
                    variant="secondary"
                    onClick={() => onChange([...fields, { name: nextHumanFieldName(fields), type: words.addType }])}
                >
                    <Plus size={12} aria-hidden /> {words.add}
                </Button>
                <AnswerPasteButtons paste={paste} setPaste={setPaste} />
            </div>
            {paste && (
                <AnswerPaste
                    key={paste}
                    kind={paste}
                    replacing={fields.length}
                    onTake={(next) => { onChange(next); setPaste(null); }}
                    onCancel={() => setPaste(null)}
                />
            )}
            {fields.length > 0 && <AnswerAskedFor fields={fields} words={words.shown} />}
        </div>
    );
}

/** "Paste an example" and "Paste a JSON Schema", each opening its paste or closing it. */
export function AnswerPasteButtons({ paste, setPaste }: {
    paste: null | 'example' | 'schema'; setPaste: (next: null | 'example' | 'schema') => void;
}) {
    return (
        <>
            <Button size="sm" variant="ghost" aria-expanded={paste === 'example'}
                    onClick={() => setPaste(paste === 'example' ? null : 'example')}>
                Paste an example
            </Button>
            <Button size="sm" variant="ghost" aria-expanded={paste === 'schema'}
                    onClick={() => setPaste(paste === 'schema' ? null : 'schema')}>
                Paste a JSON Schema
            </Button>
        </>
    );
}

/** One level of answer fields, a row each. */
function AnswerFieldRows({ fields, onChange, level, path }: {
    fields: AnswerField[]; onChange: (next: AnswerField[]) => void; level: number; path: string;
}) {
    return (
        <div className="space-y-1.5">
            {fields.map((field, idx) => (
                <AnswerFieldRow
                    key={idx}
                    field={field}
                    here={path ? `${path}.${idx + 1}` : `${idx + 1}`}
                    level={level}
                    onChange={(next) => onChange(fields.map((f, i) => (i === idx ? next : f)))}
                    onRemove={() => onChange(fields.filter((_, i) => i !== idx))}
                />
            ))}
        </div>
    );
}

/** One answer field: its name, type, a choice's options or a list's items, what it holds, whether it may be empty,
 * and a group's own rows under it, indented. `here` numbers it by its place (`1.2` is the first field's second). */
function AnswerFieldRow({ field, here, level, onChange, onRemove }: {
    field: AnswerField; here: string; level: number; onChange: (next: AnswerField) => void; onRemove: () => void;
}) {
    const kind = answerFieldKind(field);
    const rows = answerFieldChildren(field);
    const change = (patch: Partial<AnswerField>) => onChange({ ...field, ...patch });
    // A group at the fourth level would hold a fifth: offered, disabled, with why (F1618).
    const offered: { value: string; label: string; hint?: string; disabled?: boolean }[] = ANSWER_FIELD_TYPES.map((t) =>
        level >= MAX_LEVELS && (t.value === 'object' || t.value === 'groups')
            ? { ...t, disabled: true, hint: 'Four levels at most' } : t);
    // A stored spelling it knows reads as its type without being rewritten; one it does not is shown as itself,
    // never blank (F75's rule). A type the server holds that the editor does not offer is shown by its name.
    const options = kind === 'email' || kind === 'date' || kind === 'phone'
        ? [...offered, { value: kind, label: STORED_ONLY_LABELS[kind] }]
        : kind === null && field.type
            ? [...offered, { value: field.type, label: `unknown type “${field.type}”`, disabled: true }]
            : offered;
    const listOf = typeof field.of === 'string' && field.of.trim() ? field.of.trim().toLowerCase() : 'text';
    const items = listOf === 'string' ? 'text' : listOf;
    const itemOptions: { value: string; label: string; disabled?: boolean }[] = LIST_ITEM_KINDS.some((k) => k.value === items)
        ? LIST_ITEM_KINDS : [...LIST_ITEM_KINDS, { value: items, label: `unknown kind “${String(field.of)}”`, disabled: true }];
    const emptyId = `answer-field-${here}-may-be-empty`;
    return (
        <div className="space-y-1.5 rounded-md border border-border bg-surface-2/40 p-2">
            <div className="flex gap-1.5">
                <LocalInput
                    className="font-code"
                    value={field.name}
                    aria-label={`Answer field ${here} name`}
                    placeholder="field_name"
                    onCommit={(name) => change({ name })}
                />
                <Select
                    size="sm"
                    aria-label={`Answer field ${here} type`}
                    className="w-32 shrink-0"
                    value={kind ?? field.type ?? 'string'}
                    onChange={(next) => onChange(answerFieldAs(field, next))}
                    options={options}
                />
                <button
                    type="button"
                    aria-label={`Remove answer field ${here}`}
                    onClick={onRemove}
                    className="shrink-0 text-text-faint transition-colors hover:text-danger"
                >
                    <Trash2 size={12} aria-hidden />
                </button>
            </div>
            {field.name && !answerFieldNameOk(field.name) && (
                <p className="text-[11px] text-danger">Letters, digits and _ only, so a later step can read it.</p>
            )}
            {kind === 'choice' && (
                // The words it picks one of, comma-separated.
                <LocalInput
                    value={answerFieldOptions(field).join(', ')}
                    aria-label={`Answer field ${here} options`}
                    placeholder="Options, comma-separated: urgent, normal"
                    onCommit={(text) => change({ options: text.split(',').map((o) => o.trim()).filter(Boolean) })}
                />
            )}
            {kind === 'list' && (
                <div className="flex items-center gap-1.5 text-xs text-text-muted">
                    <span>Each item is</span>
                    <Select
                        size="sm"
                        aria-label={`Answer field ${here} items`}
                        className="w-28"
                        value={items}
                        onChange={(next) => change({ of: next === 'text' ? undefined : next })}
                        options={itemOptions}
                    />
                </div>
            )}
            <div className="flex items-center gap-2">
                <LocalInput
                    className="min-w-0 flex-1"
                    value={field.description ?? ''}
                    aria-label={`Answer field ${here} description`}
                    placeholder="What it holds"
                    onCommit={(description) => change({ description: description || undefined })}
                />
                {/* Itself or nothing (null): the model may leave it out, and a later step reads it empty. */}
                <span className="flex shrink-0 items-center gap-1.5">
                    <Checkbox
                        id={emptyId}
                        checked={Boolean(field.may_be_empty)}
                        onChange={(on) => change({ may_be_empty: on || undefined })}
                        label={`Answer field ${here} may be empty`}
                    />
                    <label htmlFor={emptyId} className="text-xs text-text">May be empty</label>
                </span>
            </div>
            {(kind === 'object' || kind === 'groups') && (
                <div className="space-y-1.5 border-l-2 border-border pl-2">
                    <p className="text-[11px] text-text-muted">{kind === 'groups' ? 'Each item holds:' : 'The group holds:'}</p>
                    <AnswerFieldRows
                        fields={rows}
                        onChange={(next) => onChange(withAnswerFieldChildren(field, next))}
                        level={level + 1}
                        path={here}
                    />
                    <Button
                        size="sm"
                        variant="ghost"
                        onClick={() => onChange(withAnswerFieldChildren(field, [...rows, { name: nextHumanFieldName(rows), type: 'string' }]))}
                    >
                        <Plus size={12} aria-hidden /> Add a field to {field.name || `field ${here}`}
                    </Button>
                </div>
            )}
        </div>
    );
}

/** "Paste an example" and "Paste a JSON Schema" (F1618): what is pasted becomes fields, shown before they are taken.
 * What cannot be taken is named, part by part, and then nothing of it is taken. */
export function AnswerPaste({ kind, replacing, onTake, onCancel }: {
    kind: 'example' | 'schema'; replacing: number; onTake: (fields: AnswerField[]) => void; onCancel: () => void;
}) {
    const [text, setText] = useState('');
    const [read, setRead] = useState<PastedFields | null>(null);
    const readIt = () => {
        let value: unknown;
        try {
            value = JSON.parse(text);
        } catch (err) {
            setRead({ fields: [], refused: [`It is not JSON yet: ${(err as Error).message}`] });
            return;
        }
        setRead(kind === 'example' ? answerFieldsFromExample(value) : answerFieldsFromSchema(value));
    };
    return (
        <div className="space-y-1.5 rounded-md border border-border p-2">
            <p className="text-[11px] text-text-muted">
                {kind === 'example'
                    ? 'An answer as JSON. Each value gives its field a type: "text", 3, true, a group {…}, a list of groups [{…}].'
                    : 'A JSON Schema of one object. A part the run cannot hold the model to is named, and nothing is taken until every part fits.'}
            </p>
            <Textarea
                autoFocus
                size="sm"
                rows={5}
                className="font-code text-xs"
                aria-label={kind === 'example' ? 'Example answer' : 'JSON Schema'}
                placeholder={kind === 'example' ? '{"leads": [{"email": "ana@example.com", "score": 0.9}]}' : '{"type": "object", "properties": {…}}'}
                value={text}
                onChange={(e) => { setText(e.target.value); setRead(null); }}
            />
            <div className="flex gap-1.5">
                <Button size="sm" variant="secondary" onClick={readIt} disabled={!text.trim()}>Read it</Button>
                <Button size="sm" variant="ghost" onClick={onCancel}>Cancel</Button>
            </div>
            {read && read.refused.length > 0 && (
                <div role="alert" data-testid="answer-paste-refused"
                     className="space-y-1 rounded-md border border-danger/40 bg-danger/5 p-2 text-xs text-text">
                    <p className="font-medium">
                        Nothing is taken: {read.refused.length === 1 ? 'one part does not fit.' : `${read.refused.length} parts do not fit.`}
                    </p>
                    <ul className="list-disc space-y-0.5 pl-4">
                        {read.refused.map((why, i) => <li key={i}>{why}</li>)}
                    </ul>
                </div>
            )}
            {read && read.fields.length > 0 && (
                <div data-testid="answer-paste-preview"
                     className="space-y-1.5 rounded-md border border-border bg-surface-2/40 p-2 text-xs text-text">
                    <p className="font-medium">
                        {replacing ? `These fields, in place of the ${replacing} above:` : 'These fields:'}
                    </p>
                    <AnswerFieldTree fields={read.fields} />
                    <Button size="sm" onClick={() => onTake(read.fields)}>Use these fields</Button>
                </div>
            )}
        </div>
    );
}

/** Fields as words, each group's under it. */
function AnswerFieldTree({ fields }: { fields: AnswerField[] }) {
    return (
        <ul className="space-y-0.5">
            {fields.map((field, i) => (
                <li key={i}>
                    <span className="font-code">{field.name}</span> — {answerFieldWords(field)}
                    {answerFieldChildren(field).length > 0 && (
                        <div className="mt-0.5 border-l-2 border-border pl-2">
                            <AnswerFieldTree fields={answerFieldChildren(field)} />
                        </div>
                    )}
                </li>
            ))}
        </ul>
    );
}

/** The schema the fields compile to (F1618), as the server compiles it: read-only, so what is shown is what is sent.
 * A model step's is what the run asks its model for; a tool's is its output schema (F1620). */
function AnswerAskedFor({ fields, words }: { fields: AnswerField[]; words: AnswerFieldsWords['shown'] }) {
    return (
        <details className="rounded-md border border-border">
            <summary className="cursor-pointer px-2 py-1.5 text-xs font-medium text-text">{words.title}</summary>
            <p className="px-2 text-[11px] text-text-muted">
                {words.intro}
            </p>
            <pre
                tabIndex={0}
                aria-label={words.label}
                className="max-h-72 overflow-auto px-2 pb-2 pt-1 font-code text-[11px] leading-snug text-text"
            >
                {JSON.stringify(answerJsonSchema(fields), null, 2)}
            </pre>
        </details>
    );
}
