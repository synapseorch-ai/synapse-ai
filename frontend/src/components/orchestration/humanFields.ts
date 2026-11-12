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
 * what a later step reads (`{{ state.approval.approved }}`), so name is the key.
 *
 * A stored type this table does not know is shown back as itself — never
 * replaced by a guess — and answered as text.
 */

export type HumanFieldType = 'string' | 'textarea' | 'number' | 'boolean' | 'email' | 'date' | 'phone' | 'choice';

/**
 * `options` is a dropdown's (`choice`): one of them is picked (F455). `key` is how the template packs spelt a field's
 * name until F1557: read as its name (`namedHumanFields`), never written.
 */
export type HumanField = { name: string; type?: string; label?: string; options?: string[]; key?: string };

/** What the step editor offers, in order. */
export const HUMAN_FIELD_TYPES: { value: HumanFieldType; label: string }[] = [
    { value: 'string', label: 'Text' },
    { value: 'textarea', label: 'Long text' },
    { value: 'number', label: 'Number' },
    { value: 'boolean', label: 'Yes / No' },
    // F455: the server checks these four (`core/orchestration/human_fields.py`); the four above it never did.
    { value: 'email', label: 'Email' },
    { value: 'date', label: 'Date' },
    { value: 'phone', label: 'Phone' },
    { value: 'choice', label: 'Dropdown' },
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
    select: 'choice',
    dropdown: 'choice',
    options: 'choice',
    enum: 'choice',
    tel: 'phone',
    // The planner's own spelling of a yes/no (F1434), which the clarify form already read as one.
    yes_no: 'boolean',
    yesno: 'boolean',
    'yes/no': 'boolean',
};

/** The type a field is, or `null` for one this table does not know. No type is text. */
export function humanFieldType(type: string | null | undefined): HumanFieldType | null {
    const key = (type ?? '').trim().toLowerCase();
    if (!key) return 'string';
    if (HUMAN_FIELD_TYPES.some((t) => t.value === key)) return key as HumanFieldType;
    return ALIASES[key] ?? null;
}

/** A dropdown's options, as the step wrote them, blanks left out. */
export function humanFieldOptions(field: HumanField): string[] {
    return (Array.isArray(field.options) ? field.options : []).map((o) => String(o).trim()).filter(Boolean);
}

const said = (v: unknown): v is string => typeof v === 'string' && v.trim() !== '';

/**
 * Each field named (F1557): one with no name of its own, named by its `key` (the template packs' spelling), which
 * nothing read, so the form gave every box there one answer. A field with a name keeps it. The server reads the same
 * (`human_fields.named`).
 */
export function namedHumanFields<F>(fields: F[]): F[] {
    return fields.map((f) => {
        const field = f as unknown as { name?: unknown; key?: unknown };
        return field && typeof field === 'object' && !said(field.name) && said(field.key)
            ? ({ ...field, name: field.key } as unknown as F)
            : f;
    });
}

/**
 * The name Add field gives a new field (F1557): `field_<n>` from the count up, past every name the step has. It was
 * the count alone, so after a delete it repeated one, and two boxes kept one answer.
 */
export function nextHumanFieldName(fields: { name?: string; key?: string }[]): string {
    const taken = new Set(namedHumanFields(fields).map((f) => f?.name));
    let n = fields.length + 1;
    while (taken.has(`field_${n}`)) n += 1;
    return `field_${n}`;
}

const COUNTED: Record<number, string> = { 2: 'Two', 3: 'Three', 4: 'Four' };

export type HumanFieldNameIssue =
    | { kind: 'nameless'; index: number; message: string }
    | { kind: 'repeated'; name: string; message: string };

/**
 * What is wrong with a step's field names (F1557), the repair first: a field with no name, or a name another field
 * has. The answer keeps one value per name, and the form one box's value per name, so such a field loses its answer.
 * The server says the same (`human_fields.name_issues`); `key` is the state key the step's answer is written to.
 */
export function humanFieldNameIssues(fields: HumanField[], key: string): HumanFieldNameIssue[] {
    const issues: HumanFieldNameIssue[] = [];
    const counts = new Map<string, number>();
    namedHumanFields(fields).forEach((field, i) => {
        if (!field || typeof field !== 'object') return;
        if (!said(field.name)) {
            const label = typeof field.label === 'string' ? field.label.trim() : '';
            issues.push({
                kind: 'nameless', index: i + 1,
                message: `Field ${i + 1}${label ? ` (“${label}”)` : ''} has no name: give it one — the answer keeps ` +
                    `each field under its name, and a later step reads it as state.${key}.<name>`,
            });
        } else {
            counts.set(field.name, (counts.get(field.name) ?? 0) + 1);
        }
    });
    for (const [name, count] of counts) {
        if (count > 1) {
            issues.push({
                kind: 'repeated', name,
                message: `${COUNTED[count] ?? count} fields are named “${name}”: give each a name of its own — the ` +
                    `answer keeps one value per name, so the others are lost`,
            });
        }
    }
    return issues;
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
