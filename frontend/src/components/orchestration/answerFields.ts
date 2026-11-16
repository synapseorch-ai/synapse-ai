/**
 * A model step's answer fields (F1553): what an LLM or Agent step answers when a later step reads its fields.
 *
 * The server's twin is `backend/core/orchestration/output_fields.py`. The types are a Human step's (the same
 * spellings, `humanFields.ts`), `list` (of text unless it says `of`) and `object`, a group of fields (F1618); the
 * editor offers the five a model's answer needs and the two groups. A stored type this table does not know is shown
 * back as itself, never replaced by a guess.
 *
 * `answerJsonSchema` and `answerDeclarationProblems` are the server's `json_schema` and `declaration_problems`, byte
 * for byte and word for word (a test runs both on the same declarations), so "What the model is asked for" is what
 * the run sends. `answerFieldsFromExample` and `answerFieldsFromSchema` turn a pasted JSON value or JSON Schema into
 * fields, or name each part they cannot take and take nothing.
 */
import type { OutputField } from '@/types/orchestration';
import { humanFieldOptions, humanFieldType, type HumanField } from './humanFields';

export type AnswerFieldType = 'string' | 'number' | 'boolean' | 'choice' | 'list' | 'object' | 'groups';

/** How the editor shows a stored field: one of the seven it offers, or a type the server holds that it shows by name
 * only when a step has one (`email`, `date`, `phone`). */
export type AnswerFieldKind = AnswerFieldType | 'email' | 'date' | 'phone';

export type AnswerField = OutputField;

/** What the step editor offers, in order. A Group's fields and a List of groups' are rows of their own. */
export const ANSWER_FIELD_TYPES: { value: AnswerFieldType; label: string }[] = [
    { value: 'boolean', label: 'Yes / No' },
    { value: 'choice', label: 'One of' },
    { value: 'string', label: 'Text' },
    { value: 'number', label: 'Number' },
    { value: 'list', label: 'List' },
    { value: 'object', label: 'Group' },
    { value: 'groups', label: 'List of groups' },
];

/** The types the server holds that the editor shows by name when a step has one, without offering them. */
export const STORED_ONLY_LABELS: Record<'email' | 'date' | 'phone', string> = { email: 'Email', date: 'Date', phone: 'Phone' };

/** What a List's items can be, besides groups. */
export const LIST_ITEM_KINDS: { value: 'text' | 'number' | 'boolean'; label: string }[] = [
    { value: 'text', label: 'Text' },
    { value: 'number', label: 'Number' },
    { value: 'boolean', label: 'Yes / No' },
];

/** How deep a declaration may nest, how many fields it may hold in all, and how many may be empty: the server's. */
export const MAX_LEVELS = 4;
export const MAX_FIELDS = 60;
export const MAX_MAY_BE_EMPTY = 16;

const ANSWER_TYPES = new Set(['string', 'textarea', 'number', 'boolean', 'email', 'date', 'phone', 'choice', 'list', 'object']);
const ANSWER_ALIASES: Record<string, string> = { array: 'list', strings: 'list', list_of_text: 'list', group: 'object' };
const LIST_OF: Record<string, string> = { text: 'string', string: 'string', number: 'number', boolean: 'boolean' };
const NAME = /^[A-Za-z_][A-Za-z0-9_]*$/;

type Json = Record<string, unknown>;

const isObject = (v: unknown): v is Json => typeof v === 'object' && v !== null && !Array.isArray(v);

/** Whether Python reads `v` as true: an empty list or object is false there, true here. */
const truthy = (v: unknown): boolean =>
    Array.isArray(v) ? v.length > 0 : isObject(v) ? Object.keys(v).length > 0 : Boolean(v);

const text = (v: unknown): string => (truthy(v) ? String(v) : '');

/** The server's `field_type`: the type an answer field is, or `null` for one it does not know. No type is text. */
export function answerTypeOf(raw: unknown): string | null {
    const key = text(raw).trim().toLowerCase();
    if (!key) return 'string';
    if (ANSWER_TYPES.has(key)) return key;
    if (key in ANSWER_ALIASES) return ANSWER_ALIASES[key];
    return humanFieldType(key);
}

/** The type an answer field is, among the five flat ones the editor offered first, or `null` for any other. */
export function answerFieldType(type: string | null | undefined): AnswerFieldType | null {
    const kind = answerTypeOf(type);
    if (kind === 'list') return 'list';
    if (kind === 'textarea') return 'string';
    return ['boolean', 'choice', 'string', 'number'].includes(kind ?? '') ? (kind as AnswerFieldType) : null;
}

/** How the editor shows a stored field (`AnswerFieldKind`), or `null` for a type the server does not know. */
export function answerFieldKind(field: AnswerField): AnswerFieldKind | null {
    const kind = answerTypeOf(field.type);
    if (kind === null) return null;
    if (kind === 'list') return isObject(field.of) ? 'groups' : 'list';
    if (kind === 'textarea') return 'string';
    return kind as AnswerFieldKind;
}

export function answerFieldTypeLabels(): string[] {
    return ANSWER_FIELD_TYPES.map((t) => t.label);
}

/** A choice's options, as the step wrote them, blanks left out. */
export function answerFieldOptions(field: AnswerField): string[] {
    return humanFieldOptions(field as HumanField);
}

/** A name a later step can read as `state.<key>.<name>`. */
export function answerFieldNameOk(name: string): boolean {
    return NAME.test(name.trim());
}

/** The declared fields that name something, as given: the server's `normalised`. */
function normalised(fields: unknown): AnswerField[] {
    return (Array.isArray(fields) ? fields : []).filter(
        (f): f is AnswerField => isObject(f) && typeof f.name === 'string' && f.name.trim() !== '');
}

/** A group's fields, or a List of groups' fields: the rows under a field. */
export function answerFieldChildren(field: AnswerField): AnswerField[] {
    const kind = answerFieldKind(field);
    if (kind === 'object') return Array.isArray(field.fields) ? field.fields : [];
    if (kind === 'groups' && isObject(field.of)) return Array.isArray(field.of.fields) ? field.of.fields : [];
    return [];
}

/** `field` with the rows under it replaced. */
export function withAnswerFieldChildren(field: AnswerField, children: AnswerField[]): AnswerField {
    return answerFieldKind(field) === 'groups' ? { ...field, of: { ...(field.of as object), fields: children } }
        : { ...field, fields: children };
}

/** `field` as another kind: its name, description and "may be empty" kept, a choice's options kept, and a group's
 * rows kept between Group and List of groups. A new group starts with one text field, as a group needs one. */
export function answerFieldAs(field: AnswerField, kind: string): AnswerField {
    const rest: Partial<AnswerField> = {};
    if (field.description) rest.description = field.description;
    if (field.may_be_empty) rest.may_be_empty = true;
    const rows = answerFieldChildren(field);
    const seeded = rows.length ? rows : [{ name: 'field_1', type: 'string' }];
    if (kind === 'object') return { name: field.name, type: 'object', fields: seeded, ...rest };
    if (kind === 'groups') return { name: field.name, type: 'list', of: { fields: seeded }, ...rest };
    if (kind === 'choice') {
        return { name: field.name, type: 'choice', ...(field.options ? { options: field.options } : {}), ...rest };
    }
    return { name: field.name, type: kind, ...rest };
}

// ── The schema the run sends: the server's `json_schema` ────────────────────────────────────────────────────────────

function schemaOf(field: AnswerField): Json {
    const kind = answerTypeOf(field.type) ?? 'string';
    let out: Json;
    if (kind === 'boolean') out = { type: 'boolean' };
    else if (kind === 'number') out = { type: 'number' };
    else if (kind === 'choice' && answerFieldOptions(field).length) out = { type: 'string', enum: answerFieldOptions(field) };
    else if (kind === 'list') {
        const of = field.of;
        out = isObject(of) ? { type: 'array', items: objectSchema(of.fields) }
            : { type: 'array', items: { type: LIST_OF[(of ? String(of) : 'text').trim().toLowerCase()] ?? 'string' } };
    } else if (kind === 'object') out = objectSchema(field.fields);
    else out = { type: 'string' };
    // Itself or nothing: still required, as a strict schema wants every field.
    if (truthy(field.may_be_empty)) out = { anyOf: [out, { type: 'null' }] };
    if (text(field.description).trim()) out.description = String(field.description).trim();
    return out;
}

function objectSchema(fields: unknown): Json {
    const declared = normalised(fields);
    const properties: Json = {};
    for (const f of declared) properties[f.name.trim()] = schemaOf(f);
    return { type: 'object', properties, required: declared.map((f) => f.name.trim()), additionalProperties: false };
}

/** The one object a provider's JSON schema mode is asked for: every field, no other, each group's fields the same. */
export function answerJsonSchema(fields: unknown): Json {
    return objectSchema(fields);
}

// ── What keeps a declaration from being held: the server's `declaration_problems` ─────────────────────────────────

type Counts = { fields: number; empty: number; deep: boolean };

/** A name a later step cannot read, one twice, a type the server does not know, a choice with nothing to choose
 * from, a group with no fields, a list of no kind; and past the limits. The server's sentences, word for word. */
export function answerDeclarationProblems(fields: unknown): string[] {
    const problems: string[] = [];
    const counts: Counts = { fields: 0, empty: 0, deep: false };
    problemsIn(fields, 1, '', problems, counts);
    if (counts.deep) problems.push('the answer nests deeper than four levels: flatten a group into its parent');
    if (counts.fields > MAX_FIELDS) {
        problems.push(`the answer has ${counts.fields} fields; at most ${MAX_FIELDS}: keep the ones a later step reads`);
    }
    if (counts.empty > MAX_MAY_BE_EMPTY) {
        problems.push(`${counts.empty} fields may be empty; at most ${MAX_MAY_BE_EMPTY}: make the rest always given`);
    }
    return problems;
}

function problemsIn(fields: unknown, level: number, prefix: string, problems: string[], counts: Counts): void {
    const seen = new Set<string>();
    if (level > MAX_LEVELS) counts.deep = true;
    for (const f of Array.isArray(fields) ? fields : []) {
        if (!isObject(f) || typeof f.name !== 'string' || !f.name.trim()) {
            problems.push('a field with no name: give each a `name`');
            continue;
        }
        const name = f.name.trim();
        const where = `${prefix}${name}`;
        if (!NAME.test(name)) {
            problems.push(`\`${where}\` is not a name a later step can read as state.<key>.<name>: use letters, `
                + 'digits and _ (e.g. `is_risky`)');
        }
        if (seen.has(name)) problems.push(`\`${where}\` is declared twice`);
        seen.add(name);
        if (truthy(f.may_be_empty)) counts.empty += 1;
        const kind = answerTypeOf(f.type);
        if (kind === null) {
            problems.push(`\`${where}\` has type '${String(f.type)}', which is not a type: use one of text, number, `
                + 'boolean (a yes or no), choice (with options), list, or object (a group with its own fields)');
            counts.fields += 1;
        } else if (kind === 'choice' && !answerFieldOptions(f as AnswerField).length) {
            problems.push(`\`${where}\` is a choice with no options: give it "options": ["…", "…"], one of which `
                + 'the model picks');
            counts.fields += 1;
        } else if (kind === 'object') {
            const inner = f.fields;
            if (!Array.isArray(inner) || !inner.length) problems.push(`\`${where}\` is a group with no fields: give it "fields": [...]`);
            else problemsIn(inner, level + 1, `${where}.`, problems, counts);
        } else if (kind === 'list') {
            const of = f.of;
            if (isObject(of)) {
                const inner = of.fields;
                if (!Array.isArray(inner) || !inner.length) {
                    problems.push(`\`${where}\` is a list of groups with no fields: give its "of" "fields": [...]`);
                } else problemsIn(inner, level + 1, `${where}[].`, problems, counts);
            } else if (of !== undefined && of !== null && !(String(of).trim().toLowerCase() in LIST_OF)) {
                problems.push(`\`${where}\` is a list of '${String(of)}', which is not a kind: use text, number, boolean, `
                    + 'or {"fields": [...]} for a list of groups');
                counts.fields += 1;
            } else counts.fields += 1;
        } else counts.fields += 1;
    }
}

/** A field's type in words, as the server says it to the model (`type_words`), and "or empty" when it may be. */
export function answerFieldWords(field: AnswerField): string {
    const kind = answerTypeOf(field.type) ?? 'string';
    const names = (fields: unknown) => normalised(fields).map((f) => f.name.trim()).join(', ');
    let words: string;
    if (kind === 'boolean') words = 'true or false';
    else if (kind === 'number') words = 'a number';
    else if (kind === 'choice' && answerFieldOptions(field).length) {
        words = 'one of ' + answerFieldOptions(field).map((o) => JSON.stringify(o)).join(', ');
    } else if (kind === 'list') {
        const of = field.of;
        const scalar: Record<string, string> = { number: 'a list of numbers', boolean: 'a list of true or false' };
        words = isObject(of) ? `a list of groups, each with: ${names(of.fields)}`
            : scalar[(of ? String(of) : '').trim().toLowerCase()] ?? 'a list of text';
    } else if (kind === 'object') words = `a group with: ${names(field.fields)}`;
    else if (kind === 'date') words = 'a date, YYYY-MM-DD';
    else words = 'text';
    return field.may_be_empty ? `${words}, or empty` : words;
}

// ── Paste an example ─────────────────────────────────────────────────────────────────────────────────────────────────

export type PastedFields = { fields: AnswerField[]; refused: string[] };

/** A field as it is read from a value or a schema. `_null` marks one seen only empty, until another item says what
 * it is. */
type Built = AnswerField & { _null?: boolean };

/** The fields, as a person reads them: name, type, then options, items, rows, description, may be empty. */
function ordered(field: Built): AnswerField {
    const out: AnswerField = { name: field.name, type: field.type };
    if (field.options) out.options = field.options;
    if (field.of !== undefined) out.of = isObject(field.of) ? { fields: (field.of.fields ?? []).map(ordered) } : field.of;
    if (field.fields) out.fields = field.fields.map(ordered);
    if (field.description) out.description = field.description;
    if (field.may_be_empty || field._null) out.may_be_empty = true;
    return out;
}

const SHAPE_WORDS: Record<string, string> = {
    string: 'text', number: 'a number', boolean: 'a yes or no', object: 'a group', groups: 'a list of groups',
    'list:text': 'a list of text', 'list:number': 'a list of numbers', 'list:boolean': 'a list of yes or no',
};

function shapeOf(field: Built): string {
    if (field.type === 'list') return isObject(field.of) ? 'groups' : `list:${field.of ?? 'text'}`;
    return field.type ?? 'string';
}

function merged(a: Built, b: Built, where: string, refused: string[]): Built {
    if (a._null) return { ...b, may_be_empty: true };
    if (b._null) return { ...a, may_be_empty: true };
    const sa = shapeOf(a);
    const sb = shapeOf(b);
    if (sa !== sb) {
        refused.push(`\`${where}\` is ${SHAPE_WORDS[sa] ?? sa} in one item and ${SHAPE_WORDS[sb] ?? sb} in another: `
            + 'give it one kind');
        return a;
    }
    const may = Boolean(a.may_be_empty || b.may_be_empty);
    if (sa === 'object') {
        return { ...a, fields: mergedAll(a.fields ?? [], b.fields ?? [], `${where}.`, refused), may_be_empty: may };
    }
    if (sa === 'groups') {
        const rows = (f: Built) => (isObject(f.of) ? f.of.fields ?? [] : []);
        return { ...a, of: { fields: mergedAll(rows(a), rows(b), `${where}[].`, refused) }, may_be_empty: may };
    }
    return { ...a, may_be_empty: may };
}

/** Two items' fields as one: a field one of them lacks may be empty. */
function mergedAll(a: Built[], b: Built[], prefix: string, refused: string[]): Built[] {
    const inB = new Map(b.map((f) => [f.name, f]));
    const inA = new Set(a.map((f) => f.name));
    const out = a.map((f) => {
        const other = inB.get(f.name);
        return other ? merged(f, other, `${prefix}${f.name}`, refused) : { ...f, may_be_empty: true };
    });
    for (const f of b) if (!inA.has(f.name)) out.push({ ...f, may_be_empty: true });
    return out;
}

function fromValue(name: string, value: unknown, where: string, refused: string[]): Built {
    if (value === null) return { name, type: 'string', _null: true };
    if (typeof value === 'boolean') return { name, type: 'boolean' };
    if (typeof value === 'number') return { name, type: 'number' };
    if (typeof value === 'string') return { name, type: 'string' };
    if (isObject(value)) return { name, type: 'object', fields: fromObject(value, `${where}.`, refused) };
    if (!Array.isArray(value)) {
        refused.push(`\`${where}\` is not a JSON value an answer holds`);
        return { name, type: 'string' };
    }
    if (!value.length) return { name, type: 'list' };
    if (value.some((v) => v === null)) {
        refused.push(`\`${where}\` has an empty item: a list's items are never empty, so take the null out`);
        return { name, type: 'list' };
    }
    if (value.some(Array.isArray)) {
        refused.push(`\`${where}\` is a list of lists: a list holds values or groups, one level at a time`);
        return { name, type: 'list' };
    }
    const groups = value.filter(isObject);
    if (groups.length && groups.length !== value.length) {
        refused.push(`\`${where}\` mixes groups and values: a list holds one kind`);
        return { name, type: 'list' };
    }
    if (groups.length) {
        let fields = fromObject(groups[0], `${where}[].`, refused);
        for (const item of groups.slice(1)) {
            fields = mergedAll(fields, fromObject(item, `${where}[].`, refused), `${where}[].`, refused);
        }
        return { name, type: 'list', of: { fields } };
    }
    const kinds = [...new Set(value.map((v) => typeof v))];
    if (kinds.length > 1) {
        refused.push(`\`${where}\` holds ${kinds.map((k) => SHAPE_WORDS[k] ?? k).join(' and ')}: a list holds one kind`);
        return { name, type: 'list' };
    }
    if (kinds[0] === 'number') return { name, type: 'list', of: 'number' };
    if (kinds[0] === 'boolean') return { name, type: 'list', of: 'boolean' };
    return { name, type: 'list' };
}

function fromObject(value: Json, prefix: string, refused: string[]): Built[] {
    return Object.entries(value).map(([key, v]) => fromValue(key, v, `${prefix}${key}`, refused));
}

/** Fields from an example answer: each value's type from the value (`"x"` text, `3` a number, `true` a yes or no,
 * `{…}` a group, `[{…}]` a list of groups, whose items' fields are merged, and one an item lacks or leaves `null`
 * may be empty). What it cannot take is named, and then nothing is taken. */
export function answerFieldsFromExample(value: unknown): PastedFields {
    if (!isObject(value)) {
        return { fields: [], refused: ['The answer is one object of named fields: start the example with { and end it with }.'] };
    }
    if (!Object.keys(value).length) {
        return { fields: [], refused: ['The example has no fields: give it at least one, e.g. {"summary": "…"}.'] };
    }
    const refused: string[] = [];
    const fields = fromObject(value, '', refused).map(ordered);
    if (refused.length) return { fields: [], refused };
    const problems = answerDeclarationProblems(fields);
    return problems.length ? { fields: [], refused: problems } : { fields, refused: [] };
}

// ── Paste a JSON Schema ──────────────────────────────────────────────────────────────────────────────────────────────

/** Words that describe a schema and constrain nothing: read past. */
const READ_PAST = new Set(['title', '$schema', '$id', '$comment', 'examples', 'default', '$defs', 'definitions']);
const LIMITS = new Set(['minLength', 'maxLength', 'minimum', 'maximum', 'exclusiveMinimum', 'exclusiveMaximum', 'pattern',
    'minItems', 'maxItems', 'uniqueItems', 'multipleOf', 'minProperties', 'maxProperties', 'minContains', 'maxContains']);
const SHAPES = new Set(['oneOf', 'allOf', 'anyOf', 'not', 'if', 'then', 'else']);
const OPEN = new Set(['additionalProperties', 'patternProperties', 'propertyNames', 'unevaluatedProperties']);
const FORMATS: Record<string, string> = { date: 'date', email: 'email' };
/** What each type of node may say besides `type` and `description`. */
const OWN: Record<string, Set<string>> = {
    string: new Set(['enum', 'format']), number: new Set(), integer: new Set(), boolean: new Set(),
    array: new Set(['items']), object: new Set(['properties', 'required', 'additionalProperties']),
};

const named = (where: string) => (where ? `\`${where}\`` : 'The schema');

/** Why a keyword is outside the subset, or `null` when it is inside it. */
function refusedKeyword(key: string, value: unknown, type: string | null): string | null {
    if (key === 'type' || key === 'description' || READ_PAST.has(key)) return null;
    if (key === '$ref') return 'uses $ref: write the shape out where it is used';
    if (SHAPES.has(key)) return `is one of several shapes (${key}): a field has one type, or that type or nothing (null)`;
    if (LIMITS.has(key)) return `has \`${key}\`: an answer holds no limits on lengths, sizes or values; say it in the description`;
    if (key === 'const') return 'has `const`: give it as a choice, "enum": ["…"]';
    if (key === 'additionalProperties' && value === false && type === 'object') return null;
    if (OPEN.has(key)) return `takes keys besides its own (${key}): an answer holds its named fields only`;
    if (key === 'enum') {
        const words = type === 'string' && Array.isArray(value) && value.length > 0 && value.every((v) => typeof v === 'string');
        return words ? null : 'has an enum that is not words: a choice is one of some words';
    }
    if (key === 'format' && type === 'string') {
        return String(value) in FORMATS ? null
            : `has format '${String(value)}': an answer holds the date and email formats; take it out to read it as text`;
    }
    if (type && OWN[type]?.has(key)) return null;
    return `has \`${key}\`, which an answer cannot hold`;
}

function fromNode(name: string, node: unknown, where: string, required: boolean, refused: string[]): Built | null {
    if (!isObject(node)) {
        refused.push(`${named(where)} is not a schema`);
        return null;
    }
    let mayBeEmpty = !required;
    let description = typeof node.description === 'string' ? node.description.trim() : '';
    let inner: Json = node;
    // Itself or nothing, as the run writes it (`anyOf` with null) or as a list of two types.
    if (Array.isArray(node.anyOf)) {
        const parts = node.anyOf;
        const isNull = (p: unknown) => isObject(p) && p.type === 'null' && Object.keys(p).length === 1;
        const other = parts.length === 2 && parts.some(isNull) ? parts.find((p) => !isNull(p)) : undefined;
        const outerOk = Object.keys(node).every((k) => k === 'anyOf' || k === 'description' || READ_PAST.has(k));
        if (!isObject(other) || !outerOk) {
            refused.push(`${named(where)} is one of several shapes (anyOf): a field has one type, or that type or nothing (null)`);
            return null;
        }
        inner = other;
        mayBeEmpty = true;
        if (!description && typeof other.description === 'string') description = other.description.trim();
    }
    let type: string | null = typeof inner.type === 'string' ? inner.type : null;
    if (Array.isArray(inner.type)) {
        const types = inner.type.map(String);
        const rest = types.filter((t) => t !== 'null');
        if (types.length !== 2 || rest.length !== 1) {
            refused.push(`${named(where)} has several types (${types.join(', ')}): a field has one type, or that type or null`);
            return null;
        }
        type = rest[0];
        mayBeEmpty = true;
    }
    if (type === null && isObject(inner.properties)) type = 'object';
    const reasons = Object.entries(inner).map(([k, v]) => refusedKeyword(k, v, type)).filter((r): r is string => r !== null);
    if (reasons.length) {
        refused.push(`${named(where)} ${reasons[0]}`);
        return null;
    }
    if (type === null) {
        refused.push(`${named(where)} has no type: give it one of string, number, boolean, array or object`);
        return null;
    }
    if (!(type in OWN)) {
        refused.push(`${named(where)} has type '${type}', which an answer cannot hold`);
        return null;
    }
    const out: Built = { name, type: 'string' };
    if (type === 'number' || type === 'integer') out.type = 'number';
    else if (type === 'boolean') out.type = 'boolean';
    else if (type === 'string') {
        if (Array.isArray(inner.enum)) {
            out.type = 'choice';
            out.options = inner.enum.map(String);
        } else if (typeof inner.format === 'string') out.type = FORMATS[inner.format];
    } else if (type === 'array') {
        const items = inner.items;
        if (Array.isArray(items)) {
            refused.push(`${named(where)} gives its \`items\` as a list of shapes: a list's items share one shape`);
            return null;
        }
        if (!isObject(items)) {
            refused.push(`${named(where)} is a list of anything: give its \`items\` a type`);
            return null;
        }
        const item = fromNode('item', items, `${where}[]`, true, refused);
        if (item === null) return null;
        if (item.type === 'list') {
            refused.push(`${named(where)} is a list of lists: a list holds values or groups, one level at a time`);
            return null;
        }
        if (item.may_be_empty || item.description || !['string', 'number', 'boolean', 'object'].includes(item.type ?? '')) {
            refused.push(`${named(`${where}[]`)} is not plain text, a number, a yes or no or a group: a list's items are `
                + 'one of those, never empty, and the list is described rather than its items');
            return null;
        }
        out.type = 'list';
        if (item.type === 'object') out.of = { fields: item.fields ?? [] };
        else if (item.type !== 'string') out.of = item.type;
    } else if (type === 'object') {
        const properties = inner.properties;
        if (!isObject(properties) || !Object.keys(properties).length) {
            refused.push(`${named(where)} is a group with no fields: give it properties`);
            return null;
        }
        const needed = new Set(Array.isArray(inner.required) ? inner.required.map(String) : []);
        const fields: Built[] = [];
        for (const [key, sub] of Object.entries(properties)) {
            const field = fromNode(key, sub, where ? `${where}.${key}` : key, needed.has(key), refused);
            if (field) fields.push(field);
        }
        out.type = 'object';
        out.fields = fields;
    }
    if (description) out.description = description;
    if (mayBeEmpty) out.may_be_empty = true;
    return out;
}

/** Fields from a JSON Schema of one object (or a provider's wrapper of one), when every part is inside the answer's
 * subset: groups, lists, text (one of some words, a date, an email), numbers (an integer is one) and yes or no, each
 * itself or nothing. A property that is not `required` may be empty. Every part outside the subset is named by its
 * path, and then nothing is taken. */
export function answerFieldsFromSchema(input: unknown): PastedFields {
    let schema = input;
    if (isObject(schema) && schema.type === 'json_schema' && isObject(schema.json_schema)
        && isObject(schema.json_schema.schema)) {
        schema = schema.json_schema.schema;
    } else if (isObject(schema) && !('properties' in schema) && isObject(schema.schema)) {
        schema = schema.schema;
    }
    if (!isObject(schema) || !(schema.type === 'object' || (schema.type === undefined && isObject(schema.properties)))) {
        return { fields: [], refused: ['The answer is one object of named fields: a schema whose "type" is "object", '
            + 'with "properties".'] };
    }
    const refused: string[] = [];
    const root = fromNode('answer', schema, '', true, refused);
    if (refused.length || !root) return { fields: [], refused };
    const fields = (root.fields ?? []).map(ordered);
    const problems = answerDeclarationProblems(fields);
    return problems.length ? { fields: [], refused: problems } : { fields, refused: [] };
}
