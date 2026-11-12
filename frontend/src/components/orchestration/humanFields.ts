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
export type HumanField = {
    name: string; type?: string; label?: string; options?: string[]; key?: string;
    /** A Yes / No asked as the Approve and Decline buttons (F1560): `true` asks it so, `false` keeps it in the form. */
    approval?: boolean;
};

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

/** A word of a name that makes a Yes / No an approval: approve(d), approval, accept(ed), confirm(ed), sign(ed) off. */
const APPROVAL_WORDS = /^(?:approve|approved|approval|accept|accepted|confirm|confirmed)$/;

/** A field's name in words: `plan_approved`, `approveDraft`, `sign-off` → plan approved, approve draft, sign off. */
const nameWords = (name: string) => name.replace(/([a-z0-9])([A-Z])/g, '$1 $2').toLowerCase().split(/[^a-z0-9]+/).filter(Boolean);

/** Whether a Yes / No is an approval: its author says so (`approval`), else its name does. */
export function isApproval(field: HumanField): boolean {
    if (humanFieldType(field.type) !== 'boolean') return false;
    if (typeof field.approval === 'boolean') return field.approval;
    const words = nameWords(String(field.name ?? ''));
    return words.some((w) => APPROVAL_WORDS.test(w)) || words.join(' ').includes('sign off') || words.join(' ').includes('signed off');
}

/**
 * The one approval a step asks, which the run's card asks with Approve and Decline (M2a, F1560), or `null`. It was the
 * step's one Yes / No whatever it asked, so "Urgent?" beside a name and a priority became "Urgent" and "Decline" and
 * the form lost it. Any other Yes / No is a field in the form; two approvals are neither.
 */
export function approvalField<F extends HumanField>(fields: F[]): F | null {
    const named = namedHumanFields(fields);
    const at = named.flatMap((f, i) => (!!f && typeof f === 'object' && isApproval(f) ? [i] : []));
    return at.length === 1 ? fields[at[0]] : null;  // the step's own field, so the card leaves it out of the form
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

// ── Branching on an answer (F1558) ──────────────────────────────────────────────────────────────────────────────────
//
// A Human step's fields and a model step's answer fields (F1553) share this table, and a later If/Else reads one as
// `state.<key>.<field>`. A Yes / No holds true or false (F1434, F1552) and a One of its option as written (F455), so
// those two are what a person branches on by value: the If/Else offers them (`branchFields`), writes the condition
// (`branchCondition`) and reads one written so back (`branchPicked`). `answerValueChecks` says a condition that cannot
// follow such an answer, as the server does (`step_answers.never_followed`). Import-free, so a test runs it in node.

/** A step as these read it: any step type the editor holds (an interface has no index signature, so it is cast). */
type StepLike = { id: string; name?: string; type?: string; output_key?: string };
type AnyStep = StepLike & Record<string, unknown>;
const asSteps = (steps: readonly StepLike[] | undefined): AnyStep[] =>
    ((steps || []) as unknown as AnyStep[]).filter((s): s is AnyStep => !!s && typeof s === 'object');
type Typed = { name: string; label: string; kind: HumanFieldType | null; options: string[] };

/** The fields a step declares, or `null` for one that declares none: a Human step's, a model step's answer fields. */
function typedFields(step: AnyStep): Typed[] | null {
    const raw = step.type === 'human' ? step.human_fields : step.type === 'llm' || step.type === 'agent' ? step.output_fields : null;
    if (!Array.isArray(raw)) return null;
    const fields = (step.type === 'human' ? namedHumanFields(raw) : raw)
        .filter((f): f is HumanField => !!f && typeof f === 'object' && said((f as HumanField).name))
        .map((f) => ({
            name: f.name.trim(),
            label: (typeof f.label === 'string' && f.label.trim()) || f.name.trim(),
            kind: humanFieldType(f.type),
            options: humanFieldOptions(f),
        }));
    return fields.length ? fields : null;
}

/** The keys a step writes its answer to: a Human step's to `human_response` too. */
function keysWritten(step: AnyStep): string[] {
    const own = typeof step.output_key === 'string' && step.output_key.trim() ? step.output_key.trim() : '';
    const keys = own ? [own] : step.type === 'human' ? ['human_response'] : [];
    if (step.type === 'human' && !keys.includes('human_response')) keys.push('human_response');
    return keys;
}

export type BranchField = {
    key: string;
    field: string;
    stepId: string;
    /** "Review › Approve the plan?": the step, then the field as the person or the model is asked it. */
    label: string;
    kind: 'boolean' | 'choice';
    /** Each value as the condition writes it (`True`, `"Later"`), and as a person reads it. */
    values: { value: string; label: string }[];
};

/** Every Yes / No and One of field another step asks or answers, with the values it holds. */
export function branchFields(steps: readonly StepLike[], selfId?: string): BranchField[] {
    const out: BranchField[] = [];
    for (const step of asSteps(steps)) {
        if (step.id === selfId) continue;
        const fields = typedFields(step);
        const [key] = keysWritten(step);
        if (!fields || !key) continue;
        for (const f of fields) {
            const base = { key, field: f.name, stepId: step.id, label: `${step.name || step.id} › ${f.label}` };
            if (f.kind === 'boolean') {
                out.push({ ...base, kind: 'boolean', values: [{ value: 'True', label: 'Yes' }, { value: 'False', label: 'No' }] });
            } else if (f.kind === 'choice' && f.options.length) {
                out.push({ ...base, kind: 'choice', values: f.options.map((o) => ({ value: JSON.stringify(o), label: o })) });
            }
        }
    }
    return out;
}

/** The condition that takes the True path when `choice` holds `value` (as `values` writes it). */
export function branchCondition(choice: Pick<BranchField, 'key' | 'field'>, value: string): string {
    return `state.${choice.key}.${choice.field} == ${value}`;
}

const PICKED = /^\s*state\.(\w+)\.(\w+)\s*==\s*(True|False|"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')\s*$/;

/** The field and value a condition `branchCondition` could have written picks, or `null`. */
export function branchPicked(condition: string | undefined, choices: BranchField[]): { field: BranchField; literal: string } | null {
    const m = PICKED.exec(condition || '');
    if (!m) return null;
    const field = choices.find((c) => c.key === m[1] && c.field === m[2]);
    if (!field) return null;
    let literal = m[3];
    if (literal.startsWith("'")) literal = JSON.stringify(literal.slice(1, -1).replace(/\\(['\\])/g, '$1'));
    else if (literal.startsWith('"')) {
        try { literal = JSON.stringify(JSON.parse(literal)); } catch { return null; }
    }
    return field.values.some((v) => v.value === literal) ? { field, literal } : null;
}

export type AnswerValueCheck = {
    stepId: string;
    kind: 'yes_no_word' | 'not_an_option' | 'whole';
    key: string;
    field: string | null;
    word: string | null;
    message: string;
};

type KeyFields = { writer: AnyStep; fields: Map<string, { kind: HumanFieldType | null; options: string[] }> };

/** Each key every writer of which declares its fields, with them (a field's kind where every writer agrees). */
function fieldedKeys(steps: AnyStep[]): Map<string, KeyFields> {
    const writers = new Map<string, AnyStep[]>();
    for (const s of steps) for (const key of keysWritten(s)) writers.set(key, [...(writers.get(key) || []), s]);
    const out = new Map<string, KeyFields>();
    for (const [key, ws] of writers) {
        const typed = ws.map(typedFields);
        if (typed.some((t) => t === null)) continue;
        const fields: KeyFields['fields'] = new Map();
        for (const declared of typed) {
            for (const f of declared || []) {
                const had = fields.get(f.name) ?? { kind: f.kind, options: [] };
                if (had.kind !== f.kind) had.kind = null;
                for (const o of f.options) if (!had.options.includes(o)) had.options.push(o);
                fields.set(f.name, had);
            }
        }
        out.set(key, { writer: ws[0], fields });
    }
    return out;
}

const TRUTH = ['if_condition', 'loop_while_condition', 'loop_break_condition'];
const READS = [...TRUTH, 'switch_expression'];
const KEY_RE = String.raw`state(?:\.(\w+)|\[\s*['"](\w+)['"]\s*\])`;
const FIELD_RE = String.raw`(?:\.(\w+)|\[\s*['"](\w+)['"]\s*\])`;
const WORD_RE = String.raw`(?:'((?:[^'\\]|\\.)*)'|"((?:[^"\\]|\\.)*)")`;
/** `state.k.f == 'w'`, the read standing alone (no further step, call or index on it). */
const READ_FIRST = new RegExp(String.raw`(?<![\w.])${KEY_RE}${FIELD_RE}(?![\w.(\[])\s*(?:==|!=)\s*${WORD_RE}`, 'g');
/** `'w' == state.k.f`. */
const WORD_FIRST = new RegExp(String.raw`${WORD_RE}\s*(?:==|!=)\s*${KEY_RE}${FIELD_RE}(?![\w.(\[])`, 'g');

const unescaped = (w: string) => w.replace(/\\(.)/g, '$1');

/** A whole condition written as one `{{ … }}` is the expression inside it (F1433), as the server reads it. */
function inside(expr: string): string {
    const text = expr.trim();
    if (text.startsWith('{{') && text.endsWith('}}')) {
        const within = text.slice(2, -2);
        if (!within.includes('{{') && !within.includes('}}') && within.trim()) return within.trim();
    }
    return expr;
}

const RUNS = ' It runs as it is today.';
const stepWord = (s: AnyStep) => `“${s.name || s.id}”`;

/** A condition on a Yes / No or One of answer that cannot follow it (F1558): warnings, the run unchanged. */
export function answerValueChecks(steps: readonly StepLike[]): AnswerValueCheck[] {
    const all = asSteps(steps);
    const known = fieldedKeys(all);
    const out: AnswerValueCheck[] = [];
    if (!known.size) return out;
    const push = (check: AnswerValueCheck) => {
        if (!out.some((c) => c.stepId === check.stepId && c.kind === check.kind && c.key === check.key &&
            c.field === check.field && c.word === check.word)) out.push(check);
    };
    for (const s of all) {
        for (const name of READS) {
            const expr = s[name];
            if (typeof expr !== 'string' || !expr.trim()) continue;
            const text = inside(expr);
            const compared: [string, string, string][] = [];
            for (const m of text.matchAll(READ_FIRST)) compared.push([m[1] || m[2], m[3] || m[4], unescaped(m[5] ?? m[6])]);
            for (const m of text.matchAll(WORD_FIRST)) compared.push([m[3] || m[4], m[5] || m[6], unescaped(m[1] ?? m[2])]);
            for (const [key, fieldName, word] of compared) {
                const field = known.get(key)?.fields.get(fieldName);
                if (!field) continue;
                if (field.kind === 'boolean') {
                    push({
                        stepId: s.id, kind: 'yes_no_word', key, field: fieldName, word,
                        message: `Compares state.${key}.${fieldName}, a Yes / No, with “${word}”: it holds true or false, ` +
                            `so the comparison never follows the answer — compare it with True or False.` + RUNS,
                    });
                } else if (field.kind === 'choice' && field.options.length && !field.options.includes(word)) {
                    const near = field.options.find((o) => o.trim().toLowerCase() === word.trim().toLowerCase());
                    push({
                        stepId: s.id, kind: 'not_an_option', key, field: fieldName, word,
                        message: `Compares state.${key}.${fieldName}, one of ${field.options.map((o) => `“${o}”`).join(', ')}, ` +
                            `with “${word}”: the answer is the option as written, so the comparison never follows it — ` +
                            (near ? `use “${near}”.` : 'compare it with one of them.') + RUNS,
                    });
                }
            }
            if (!TRUTH.includes(name)) continue;
            let top = text.trim();
            const negated = /^not(?:\s+|(?=\())/.exec(top);
            if (negated) top = top.slice(negated[0].length).trim();
            const whole = new RegExp(String.raw`^${KEY_RE}$`).exec(top)
                ?? new RegExp(String.raw`^${KEY_RE}\s*(?:==|!=|is\s+not|is)\s*(?:True|False)$`).exec(top)
                ?? new RegExp(String.raw`^(?:True|False)\s*(?:==|!=|is\s+not|is)\s*${KEY_RE}$`).exec(top);
            const key = whole ? whole[1] || whole[2] : null;
            const fielded = key ? known.get(key) : undefined;
            if (key && fielded) {
                const names = [...fielded.fields.keys()];
                const first = names.find((n) => fielded.fields.get(n)?.kind === 'boolean') ?? names[0] ?? '<field>';
                push({
                    stepId: s.id, kind: 'whole', key, field: null, word: null,
                    message: `Decides on state.${key} whole, ${stepWord(fielded.writer)}’s answer: a mapping of its fields, ` +
                        `never empty and never True itself, so this never follows it — read one of its fields, e.g. ` +
                        `state.${key}.${first}.` + RUNS,
                });
            }
        }
    }
    return out;
}
