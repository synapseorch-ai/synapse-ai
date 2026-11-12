/**
 * What a step reads out of another step's answer, checked as it is edited (F1553's follow-up, Naveen's pick 3): the
 * editor says what the server says (`builder_tools._fields_never_written`, `_decisions_on_words`), from one place.
 *
 * - A field its writer never writes (F1549): a Human step answers its fields by name (none: the person's words), an
 *   Evaluator its route's label, an If/Else true or false, a Retrieve or an Ingest step its contract.
 * - A field a model step does not declare (F1553): an LLM step answers its answer fields (`output_fields`), and its
 *   words without them; an Agent step the same when its agent is known and is not a workflow.
 * - A decision on a model's own words: a condition calling a text method on such an answer, comparing it with a quoted
 *   word, or asking whether a word is in it. A warning with its fix; the run is unchanged.
 *
 * Self-contained (type imports only) so a test runs it in node beside the server's verdicts on the same workflows.
 * The server reads a condition with Python's parser; this reads the shapes a condition is written in.
 */
import type { StepConfig } from '@/types/orchestration';

export type FieldCheck = { stepId: string; key: string; field: string | null; kind: 'field' | 'words'; message: string };
type Agent = { id: string; type?: string };
type AnyStep = StepConfig & Record<string, unknown>;

/** Methods only text has: a call of one on a value is no field of it. Mirrors `compile.TEXT_METHODS` and more. */
const TEXT_METHODS = ['split', 'rsplit', 'strip', 'lstrip', 'rstrip', 'lower', 'upper', 'casefold', 'replace',
    'startswith', 'endswith', 'splitlines', 'partition', 'rpartition', 'encode', 'find', 'rfind', 'count', 'index',
    'title', 'capitalize'];
const TEXT = new Set(TEXT_METHODS);
const CONDITIONS = ['if_condition', 'switch_expression', 'loop_while_condition', 'loop_break_condition', 'loop_over'];
const DECIDING = ['if_condition', 'switch_expression', 'loop_while_condition', 'loop_break_condition'];
const RETRIEVE_FIELDS = ['context', 'chunks', 'citations', 'trace_id', 'snapshot_ids'];
const INGEST_FIELDS = ['kb_id', 'documents'];

/**
 * Each field's name. A Human field the template packs named by `key` is named by its key (`keyed`, F1557), as
 * `humanFields.namedHumanFields` and the server's `human_fields.named` read it.
 */
const names = (fields: unknown, keyed = false): string[] =>
    (Array.isArray(fields) ? fields : [])
        .map((f) => {
            if (!f || typeof f !== 'object') return '';
            const { name, key } = f as { name?: unknown; key?: unknown };
            return String(name ?? '').trim() || (keyed && typeof key === 'string' ? key.trim() : '');
        })
        .filter(Boolean);

/** Every field the step's answer has; `[]` for an answer with none; `null` when the step does not say. */
export function answerFields(step: AnyStep, agents?: Agent[]): string[] | null {
    const declared = names(step.output_fields);
    if ((step.type === 'llm' || step.type === 'agent') && declared.length) return declared;
    if (step.type === 'llm') return [];
    if (step.type === 'agent') {
        const agent = agents?.find((a) => a.id === step.agent_id);
        if (!agent || (step.finish_tools?.length ?? 0) > 0 || agent.type === 'orchestrator') return null;
        return [];
    }
    if (step.type === 'human') return names(step.human_fields, true);
    if (step.type === 'evaluator' || step.type === 'if_else') return [];
    if (step.type === 'retrieve') return RETRIEVE_FIELDS;
    if (step.type === 'ingest') return INGEST_FIELDS;
    return null;
}

function keysWritten(step: AnyStep): string[] {
    const key = step.output_key || (step.type === 'human' ? 'human_response' : '');
    const keys = key ? [key] : [];
    if (step.type === 'human' && !keys.includes('human_response')) keys.push('human_response');
    return keys;
}

/** A whole condition written as one `{{ … }}` is the expression inside it (F1433), as the server reads it. */
function unbraced(expr: string): string {
    const text = expr.trim();
    if (text.startsWith('{{') && text.endsWith('}}')) {
        const inside = text.slice(2, -2);
        if (!inside.includes('{{') && !inside.includes('}}') && inside.trim()) return inside.trim();
    }
    return expr;
}

const KEY = String.raw`state(?:\.(\w+)|\[\s*['"](\w+)['"]\s*\])`;
const FIELD_READ = new RegExp(KEY + String.raw`(?:\.(\w+)(?!\w|\s*\()|\[\s*['"](\w+)['"]\s*\]|\.get\(\s*['"](\w+)['"])`, 'g');
const TEMPLATE = /\{\{\s*state\.([\w][\w.-]*)\s*\}\}/g;
const OLD_TEMPLATE = /(?<!\{)\{state\.([\w][\w.]*)\}(?!\})/g;

/** The fields code (a condition, a Transform) reads out of each of `keys`: `state.k.f`, `state['k']['f']`, `.get('f')`. */
function codeReads(code: string, keys: Set<string>, add: (key: string, field: string) => void) {
    for (const m of code.matchAll(FIELD_READ)) {
        const key = m[1] || m[2];
        const field = m[3] || m[4] || m[5];
        if (key && field && keys.has(key) && !TEXT.has(field)) add(key, field);
    }
}

/** The fields `{{ state.k.f }}` templates read, anywhere in a value. */
function templateReads(value: unknown, keys: Set<string>, add: (key: string, field: string) => void) {
    if (typeof value === 'string') {
        if (!value.includes('state.')) return;
        for (const pattern of [TEMPLATE, OLD_TEMPLATE]) {
            for (const m of value.matchAll(pattern)) {
                const [head, field] = m[1].split('.');
                if (keys.has(head) && field && !/^-?\d+$/.test(field) && !TEXT.has(field)) add(head, field);
            }
        }
    } else if (Array.isArray(value)) {
        for (const v of value) templateReads(v, keys, add);
    } else if (value && typeof value === 'object') {
        for (const v of Object.values(value)) templateReads(v, keys, add);
    }
}

const ticked = (list: string[]) => list.map((n) => `“${n}”`).join(', ');

function repair(writer: AnyStep, key: string, field: string, has: string[]): string {
    const name = `“${writer.name || writer.id}”`;
    switch (writer.type) {
        case 'llm':
        case 'agent':
            return `${name} does not declare it in its answer fields: declare “${field}” there (Yes / No for a yes ` +
                `or no, One of for a few words) and the run holds its model to it` +
                (has.length ? `, or read one it declares (${ticked(has)})` : `, or read state.${key} itself, its text`);
        case 'human':
            return has.length
                ? `human step ${name} never writes it: read one of its fields (${ticked(has)}), or add a field named “${field}”`
                : `human step ${name} never writes it: give it a field named “${field}” (Yes / No for a yes or no), ` +
                  `or read what it writes. With no fields, its answer is the person's words`;
        case 'evaluator':
            return `evaluator ${name} never writes it: read state.${key} itself, the label of the route it took`;
        case 'if_else':
            return `if/else ${name} never writes it: read state.${key} itself, true or false`;
        default:
            return `${name} never writes it: read one of its fields (${ticked(has)})`;
    }
}

/** A decision on `keys` read as words: the shapes `output_fields.words_read` finds with Python's parser. */
function wordsRead(expr: string, keys: Set<string>): string[] {
    const text = unbraced(expr);
    const methods = TEXT_METHODS.join('|');
    const whole = String.raw`(?:str\(\s*)?${KEY}(?:\s*\))?`;
    const patterns = [
        new RegExp(whole + String.raw`(?:\s*\.\s*(?:${methods})\s*\([^()]*\))*\s*\.\s*(?:${methods})\s*\(`, 'g'),
        new RegExp(whole + String.raw`\s*(?:==|!=)\s*['"]`, 'g'),
        new RegExp(String.raw`['"]\s*(?:==|!=)\s*` + whole + String.raw`(?![\w.[])`, 'g'),
        new RegExp(String.raw`['"]\s+(?:not\s+)?in\s+` + whole, 'g'),
        new RegExp(whole + String.raw`\s+(?:not\s+)?in\s*[[({]\s*['"]`, 'g'),
    ];
    const found: string[] = [];
    for (const pattern of patterns) {
        for (const m of text.matchAll(pattern)) {
            const key = m[1] || m[2];
            if (key && keys.has(key) && !found.includes(key)) found.push(key);
        }
    }
    return found;
}

export function fieldChecks(steps: StepConfig[], agents?: Agent[]): FieldCheck[] {
    const all = (steps || []).filter((s): s is AnyStep => !!s && typeof s === 'object') as AnyStep[];
    const writers = new Map<string, AnyStep[]>();
    for (const s of all) for (const key of keysWritten(s)) writers.set(key, [...(writers.get(key) || []), s]);
    const known = new Map<string, AnyStep[]>();
    for (const [key, ws] of writers) if (ws.every((w) => answerFields(w, agents) !== null)) known.set(key, ws);
    const out: FieldCheck[] = [];

    for (const s of all) {
        const reads = new Map<string, string[]>();
        const add = (key: string, field: string) => {
            if (!known.has(key)) return;
            const list = reads.get(key) || [];
            if (!list.includes(field)) reads.set(key, [...list, field]);
        };
        for (const f of CONDITIONS) if (typeof s[f] === 'string') codeReads(unbraced(s[f] as string), new Set(known.keys()), add);
        if (typeof s.transform_code === 'string') codeReads(s.transform_code, new Set(known.keys()), add);
        const rest: Record<string, unknown> = {};
        for (const [k, v] of Object.entries(s)) if (k !== 'transform_code' && !CONDITIONS.includes(k)) rest[k] = v;
        templateReads(rest, new Set(known.keys()), add);
        for (const [key, fields] of reads) {
            const ws = known.get(key)!;
            const has = [...new Set(ws.flatMap((w) => answerFields(w, agents) || []))];
            for (const field of fields) {
                if (has.includes(field)) continue;
                const why = ws.length === 1
                    ? repair(ws[0], key, field, has)
                    : `the steps that write it (${ticked(ws.map((w) => w.name || w.id))}) never write it: ` +
                      (has.length ? `read one they write (${ticked(has)})` : `read state.${key} itself`);
                out.push({ stepId: s.id, key, field, kind: 'field', message: `Reads “${field}” from state.${key}, which ${why}` });
            }
        }
    }

    // A decision on a model's own words: every writer of the key is a model step answering its words.
    const texts = new Map<string, AnyStep>();
    for (const [key, ws] of writers) {
        if (ws.every((w) => (w.type === 'llm' || w.type === 'agent') && answerFields(w, agents)?.length === 0)) {
            texts.set(key, ws[0]);
        }
    }
    if (texts.size) {
        for (const s of all) {
            const seen = new Set<string>();
            for (const f of DECIDING) {
                const expr = s[f];
                if (typeof expr !== 'string' || !expr.trim()) continue;
                for (const key of wordsRead(expr, new Set(texts.keys()))) {
                    if (seen.has(key)) continue;
                    seen.add(key);
                    const w = texts.get(key)!;
                    out.push({
                        stepId: s.id, key, field: null, kind: 'words',
                        message: `Decides on “${w.name || w.id}”’s answer as text: it reads the model's own words; ` +
                            `declare an answer field on “${w.name || w.id}” (One of, with the words it picks from, or ` +
                            `Yes / No) and read state.${key}.<field>. It runs as it is today.`,
                    });
                }
            }
        }
    }
    return out;
}
