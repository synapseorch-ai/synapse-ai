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

import { Checkbox, Field, Input, Textarea } from '@/components/ui';
import { humanFieldLabel, humanFieldType, type HumanField, type HumanValues } from './humanFields';

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
                        {type === 'textarea' ? (
                            <Textarea rows={3} value={text} onChange={(e) => set(field.name, e.target.value)} />
                        ) : (
                            <Input
                                type={type === 'number' ? 'number' : 'text'}
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
