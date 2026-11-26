'use client';

/**
 * The fields a human step asks for, as a person answers them.
 *
 * Controlled and button-less on purpose: the orchestration screen submits from
 * its modal's footer, chat from its own card, and cloud's run page (batch 4)
 * from a third place. Each keeps its button; all of them render the fields
 * from the one table in `humanFields.ts` and send `humanAnswer(...)`, keyed by
 * name (F75).
 */

import { useLayoutEffect, useRef, type ComponentProps } from 'react';
import { Checkbox, Field, Input, Select, Textarea } from '@/components/ui';
import {
    HUMAN_FIELD_MAX_ROWS,
    humanFieldDrawsLines,
    humanFieldLabel,
    humanFieldOptions,
    humanFieldRows,
    humanFieldType,
    type HumanField,
    type HumanValues,
} from './humanFields';

/** The browser's input for a typed field: its own keyboard and checks (F455). */
const INPUT_TYPE: Record<string, string> = { number: 'number', email: 'email', date: 'date', phone: 'tel' };

/**
 * A box of lines as tall as the value it starts with (F1759): a row per line of it, then, as drawn, a row more for each
 * line that wraps, up to `HUMAN_FIELD_MAX_ROWS`, past which it scrolls. Sized by the starting value only, never by what
 * is typed, so the card does not jump while a person edits; an empty box keeps its 3 rows.
 */
function LinesBox({
    field,
    onChange,
    ...props
}: Omit<ComponentProps<typeof Textarea>, 'onChange' | 'rows' | 'ref'> & {
    field: HumanField;
    value: string;
    onChange: (value: string) => void;
}) {
    const box = useRef<HTMLTextAreaElement>(null);
    const start = typeof field.default === 'string' ? field.default : '';
    useLayoutEffect(() => {
        const el = box.current;
        while (el && el.scrollHeight > el.clientHeight && el.rows < HUMAN_FIELD_MAX_ROWS) el.rows += 1;
    }, [start]);
    // What a Field threads onto its control (a hint's aria-describedby, an error's invalid) goes on to the box.
    return <Textarea {...props} ref={box} rows={humanFieldRows(field)} onChange={(e) => onChange(e.target.value)} />;
}

export function HumanFieldsForm({
    fields,
    values,
    onChange,
    idPrefix = 'human-field',
}: {
    fields: HumanField[];
    values: HumanValues;
    onChange: (values: HumanValues) => void;
    /** Unique per form on the page, for the checkbox captions' `htmlFor`. */
    idPrefix?: string;
}) {
    const set = (name: string, value: string | boolean) => onChange({ ...values, [name]: value });

    return (
        <div className="space-y-3">
            {fields.map((field, idx) => {
                const label = humanFieldLabel(field);
                const type = humanFieldType(field.type);
                const value = values[field.name];
                if (type === 'boolean') {
                    const id = `${idPrefix}-${idx}`;
                    return (
                        <div key={idx} className="flex items-center gap-2">
                            <Checkbox id={id} checked={value === true} onChange={(checked) => set(field.name, checked)} label={label} />
                            <label htmlFor={id} className="cursor-pointer text-sm text-text">{label}</label>
                        </div>
                    );
                }
                const text = typeof value === 'string' ? value : '';
                return (
                    <Field key={idx} label={label}>
                        {/* A Text field starting with lines is a box of lines too: one line would drop them (F1759). */}
                        {humanFieldDrawsLines(field) ? (
                            <LinesBox field={field} value={text} onChange={(lines) => set(field.name, lines)} />
                        ) : type === 'choice' ? (
                            // One option is picked (Naveen, F455): a dropdown, not a list of checkboxes.
                            <Select
                                aria-label={label}
                                value={text || undefined}
                                onChange={(choice) => set(field.name, choice)}
                                placeholder="Choose…"
                                options={humanFieldOptions(field).map((o) => ({ value: o, label: o }))}
                            />
                        ) : (
                            <Input
                                type={INPUT_TYPE[type ?? ''] ?? 'text'}
                                value={text}
                                onChange={(e) => set(field.name, e.target.value)}
                            />
                        )}
                    </Field>
                );
            })}
        </div>
    );
}
