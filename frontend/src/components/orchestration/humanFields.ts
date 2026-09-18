/**
 * A human step's structured fields — one table for every form that shows or
 * answers one.
 *
 * There were four, with four ideas of a field (F75): the canvas offered three
 * types and showed anything else — including the AI builder's own
 * `textarea` — as a blank picker (F50's class of fault); the orchestration
 * screen ignored the fields and took one line of text; chat reused the
 * `collect_data` tool's form, which keys an answer by *label* and renders a
 * yes/no as a text box; cloud's run page knew only `number`. The engine, cloud's
 * run page and a channel reply all key the answer by **name**, and a name is
 * what a later step reads (`{state.approval.approved}`), so name is the key.
 *
 * A stored type this table does not know is shown back as itself — never
 * replaced by a guess — and answered as text.
 */

export type HumanFieldType = 'string' | 'textarea' | 'number' | 'boolean';

export type HumanField = { name: string; type?: string; label?: string };

/** What the step editor offers, in order. */
export const HUMAN_FIELD_TYPES: { value: HumanFieldType; label: string }[] = [
    { value: 'string', label: 'Text' },
    { value: 'textarea', label: 'Long text' },
    { value: 'number', label: 'Number' },
    { value: 'boolean', label: 'Yes / No' },
];

/** Spellings written before this table, by hand or by a model — read, never rewritten. */
const ALIASES: Record<string, HumanFieldType> = {
    text: 'string',
    str: 'string',
    long_text: 'textarea',
    bool: 'boolean',
    int: 'number',
    integer: 'number',
    float: 'number',
};

/** The type a field is, or `null` for one this table does not know. No type is text. */
export function humanFieldType(type: string | null | undefined): HumanFieldType | null {
    const key = (type ?? '').trim().toLowerCase();
    if (!key) return 'string';
    if (HUMAN_FIELD_TYPES.some((t) => t.value === key)) return key as HumanFieldType;
    return ALIASES[key] ?? null;
}

/** What a person is shown: the label, else the name. */
export function humanFieldLabel(field: HumanField): string {
    return field.label?.trim() || field.name;
}

export type HumanValues = Record<string, string | boolean>;

/** An unanswered form: every flag off, every text empty. */
export function initialHumanValues(fields: HumanField[]): HumanValues {
    const values: HumanValues = {};
    for (const field of fields) values[field.name] = humanFieldType(field.type) === 'boolean' ? false : '';
    return values;
}

/** The answer the engine stores, keyed by name: numbers as numbers, flags as booleans. */
export function humanAnswer(fields: HumanField[], values: HumanValues): Record<string, string | number | boolean | null> {
    const answer: Record<string, string | number | boolean | null> = {};
    for (const field of fields) {
        const raw = values[field.name];
        const type = humanFieldType(field.type);
        if (type === 'boolean') answer[field.name] = raw === true;
        else if (type === 'number') answer[field.name] = raw === '' || raw === undefined ? null : Number(raw);
        else answer[field.name] = typeof raw === 'string' ? raw : '';
    }
    return answer;
}
