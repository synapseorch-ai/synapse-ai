/**
 * A model step's answer fields (F1553): what an LLM or Agent step answers when a later step reads its fields.
 *
 * The server's twin is `backend/core/orchestration/output_fields.py`. The types are a Human step's (the same
 * spellings, `humanFields.ts`) and `list`, a list of text; the editor offers the five a model's answer needs. A
 * stored type this table does not know is shown back as itself, never replaced by a guess.
 */
import { humanFieldOptions, humanFieldType, type HumanField } from './humanFields';

export type AnswerFieldType = 'string' | 'number' | 'boolean' | 'choice' | 'list';

export type AnswerField = { name: string; type?: string; description?: string; options?: string[] };

/** What the step editor offers, in order. */
export const ANSWER_FIELD_TYPES: { value: AnswerFieldType; label: string }[] = [
    { value: 'boolean', label: 'Yes / No' },
    { value: 'choice', label: 'One of' },
    { value: 'string', label: 'Text' },
    { value: 'number', label: 'Number' },
    { value: 'list', label: 'List' },
];

const LIST_ALIASES = new Set(['list', 'array', 'strings', 'list_of_text']);

/** The type an answer field is, among the five the editor offers, or `null` for one it does not show. */
export function answerFieldType(type: string | null | undefined): AnswerFieldType | null {
    const key = (type ?? '').trim().toLowerCase();
    if (LIST_ALIASES.has(key)) return 'list';
    const human = humanFieldType(key);
    if (human === 'textarea') return 'string';
    return ANSWER_FIELD_TYPES.some((t) => t.value === human) ? (human as AnswerFieldType) : null;
}

/** A choice's options, as the step wrote them, blanks left out. */
export function answerFieldOptions(field: AnswerField): string[] {
    return humanFieldOptions(field as HumanField);
}

/** A name a later step can read as `state.<key>.<name>`. */
export function answerFieldNameOk(name: string): boolean {
    return /^[A-Za-z_][A-Za-z0-9_]*$/.test(name.trim());
}
