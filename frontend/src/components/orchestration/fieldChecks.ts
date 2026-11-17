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

export type FieldCheck = { stepId: string; key: string; field: string | null; kind: 'field' | 'words' | 'list' | 'way'; message: string };
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

/** The spellings a list answer field is written in: `output_fields._ANSWER_ALIASES`' lists. */
const LIST_TYPES = new Set(['list', 'array', 'strings', 'list_of_text']);
const RETRIEVE_TYPES: Record<string, string> = { context: 'string', chunks: 'list', citations: 'list', trace_id: 'string',
    snapshot_ids: 'object' };
const INGEST_TYPES: Record<string, string> = { kb_id: 'string', documents: 'list' };

/** Each field of the step's answer with `list` for a list, or null when the step does not say
 *  (`step_answers.typed_answer`): a model step with no declared fields answers text, a Tool whatever it answers. */
function typedAnswer(step: AnyStep): Record<string, string> | null {
    const typeOf = (f: unknown) => {
        const t = String((f as { type?: unknown })?.type ?? '').trim().toLowerCase();
        return LIST_TYPES.has(t) ? 'list' : t || 'string';
    };
    const named = (fields: unknown, keyed = false) => {
        const out: Record<string, string> = {};
        for (const f of Array.isArray(fields) ? fields : []) {
            const n = names([f], keyed)[0];
            if (n) out[n] = typeOf(f);
        }
        return out;
    };
    if (step.type === 'llm' || step.type === 'agent') {
        const declared = named(step.output_fields);
        return Object.keys(declared).length ? declared : null;
    }
    if (step.type === 'human') return named(step.human_fields, true);
    if (step.type === 'retrieve') return RETRIEVE_TYPES;
    if (step.type === 'ingest') return INGEST_TYPES;
    return null;
}

// ── The ways a run takes (F1621): `core/orchestration/state_reads.py`'s walk, for the editor ────────────────────────

type Steps = Map<string, StepConfig>;

/** The steps each branch runs, as the engine resolves them: a branch listing one
 *  step follows its next-step chain up to the Parallel's own next step. */
export function branchChains(parallel: StepConfig, byId: Steps): string[][] {
    return (parallel.parallel_branches || []).map((branch) => {
        if (branch.length !== 1) return [...branch];
        const chain = [branch[0]];
        let current = branch[0];
        for (;;) {
            const nxt = byId.get(current)?.next_step_id;
            if (!nxt || nxt === parallel.next_step_id || chain.includes(nxt)) break;
            chain.push(nxt);
            current = nxt;
        }
        return chain;
    });
}

/** The keys a step writes: its output key, and a Loop's or a nested Parallel's body. */
export function containerWrites(id: string, byId: Steps, seen: Set<string>): Set<string> {
    const step = byId.get(id);
    const keys = new Set<string>();
    if (!step || seen.has(id)) return keys;
    seen.add(id);
    if (step.output_key) keys.add(step.output_key);
    const inner = step.type === 'loop' ? (step.loop_step_ids || [])
        : step.type === 'parallel' ? branchChains(step, byId).flat() : [];
    for (const sub of inner) for (const k of containerWrites(sub, byId, seen)) keys.add(k);
    return keys;
}

/** What a step writes itself (not its body): its output key, a Loop's index and item, a Human step's answer. */
export function ownWrites(step: StepConfig): Set<string> {
    const keys = new Set<string>(step.output_key ? [step.output_key] : []);
    if (step.type === 'loop') {
        keys.add(step.loop_index_key || 'loop_index');
        if (step.loop_over) keys.add(step.loop_item_key || 'loop_item');
    }
    if (step.type === 'human') keys.add('human_response');
    return keys;
}

/** The steps a run can reach before any step writes `key`. A Loop's body is taken to run; a Parallel runs every
 *  branch before it goes on, so a branch step never goes on to the join (F1621): the Parallel does. */
export function firstUnwritten(key: string, entry: string | null | undefined, byId: Steps): Set<string> {
    const inLoop = new Map<string, string | null>();
    const inBranch = new Map<string, string | null>();
    for (const s of byId.values()) {
        if (s.type === 'loop') {
            const body = (s.loop_step_ids || []).filter((b) => byId.has(b));
            body.forEach((sid, i) => inLoop.set(sid, body[i + 1] ?? null));
        } else if (s.type === 'parallel') {
            for (const chain of branchChains(s, byId)) {
                chain.forEach((sid, i) => { if (!inBranch.has(sid)) inBranch.set(sid, chain[i + 1] ?? null); });
            }
        }
    }
    const reached = new Set<string>();
    const queue = entry && byId.has(entry) ? [entry] : [];
    while (queue.length) {
        const sid = queue.pop()!;
        if (reached.has(sid)) continue;
        reached.add(sid);
        const step = byId.get(sid)!;
        if (ownWrites(step).has(key)) continue;
        const targets: (string | null | undefined)[] = [];
        if (step.type === 'loop') targets.push((step.loop_step_ids || []).filter((b) => byId.has(b))[0]);
        else if (step.type === 'parallel') targets.push(...branchChains(step, byId).map((c) => c[0]));
        const container = step.type === 'loop' || step.type === 'parallel';
        if (!container || !containerWrites(sid, byId, new Set()).has(key)) {
            if (inLoop.has(sid)) targets.push(inLoop.get(sid));
            else targets.push(
                inBranch.has(sid) ? inBranch.get(sid) : step.next_step_id, step.if_true_step_id, step.if_false_step_id,
                step.switch_default_step_id, step.on_exhausted_step_id, ...Object.values(step.route_map || {}),
                ...Object.values(step.switch_cases || {}),
            );
        }
        for (const t of targets) if (t && byId.has(t) && !reached.has(t)) queue.push(t);
    }
    return reached;
}

/** A step and every step inside it: a Loop's body, a Parallel's branches. */
function contained(id: string, byId: Steps, seen: Set<string>): Set<string> {
    const step = byId.get(id);
    const found = new Set<string>();
    if (!step || seen.has(id)) return found;
    seen.add(id);
    found.add(id);
    const inner = step.type === 'loop' ? (step.loop_step_ids || [])
        : step.type === 'parallel' ? branchChains(step, byId).flat() : [];
    for (const sub of inner) for (const k of contained(sub, byId, seen)) found.add(k);
    return found;
}

/** One workflow's ways from its entry: for each key, the writers a run can bring to each step (`state_reads.Walk`).
 *  A Loop's body runs in order and round again; a value written in a Parallel's branch goes on to its join, and one
 *  written before it goes past only when no branch writes the key again. */
export class Walk {
    private readonly byId: Steps;
    private readonly cache = new Map<string, Map<string, Set<string>>>();
    private shape: { bodyNext: Map<string, string[]>; branchNext: Map<string, [string | null, string]>;
        members: Map<string, Set<string>> } | null = null;

    private readonly entry: string | null | undefined;

    constructor(steps: StepConfig[], entry: string | null | undefined) {
        this.byId = new Map(steps.filter((s) => s && s.id).map((s) => [s.id, s]));
        this.entry = entry;
    }

    reaching(key: string): Map<string, Set<string>> {
        let found = this.cache.get(key);
        if (found) return found;
        found = new Map();
        for (const writer of this.byId.values()) {
            if (!ownWrites(writer).has(key)) continue;
            const seen = new Set<string>();
            const queue = this.on(writer.id, key, writer.id);
            while (queue.length) {
                const sid = queue.pop()!;
                if (seen.has(sid)) continue;
                seen.add(sid);
                if (!found.has(sid)) found.set(sid, new Set());
                found.get(sid)!.add(writer.id);
                if (!ownWrites(this.byId.get(sid)!).has(key)) queue.push(...this.on(sid, key, writer.id));
            }
        }
        this.cache.set(key, found);
        return found;
    }

    /** The writers of `key` a read in `sid` is judged by: those a run can bring to it, or all of them when none can. */
    judgedBy<T extends { id: string }>(sid: string, key: string, candidates: T[]): T[] {
        if (!this.entry || !this.byId.has(this.entry)) return candidates;
        const reach = this.reaching(key).get(sid);
        const picked = reach ? candidates.filter((w) => reach.has(w.id)) : [];
        return picked.length ? picked : candidates;
    }

    private on(sid: string, key: string, origin: string): string[] {
        const { bodyNext, branchNext, members } = this.structure();
        const step = this.byId.get(sid)!;
        let targets: (string | null | undefined)[];
        if (bodyNext.has(sid)) targets = bodyNext.get(sid)!;
        else if (step.type === 'loop') {
            const body = (step.loop_step_ids || []).filter((b) => this.byId.has(b));
            targets = body.length ? [body[0]] : [step.next_step_id];
        } else if (step.type === 'parallel') {
            targets = branchChains(step, this.byId).filter((c) => c.length).map((c) => c[0]);
            if (!containerWrites(sid, this.byId, new Set()).has(key)) targets.push(step.next_step_id);
        } else {
            let nxt: string | null | undefined = step.next_step_id;
            const inBranch = branchNext.get(sid);
            if (inBranch) {
                nxt = inBranch[0] ?? (members.get(inBranch[1])?.has(origin) ? this.byId.get(inBranch[1])?.next_step_id : null);
            }
            targets = [nxt, step.if_true_step_id, step.if_false_step_id, step.switch_default_step_id,
                step.on_exhausted_step_id, ...Object.values(step.route_map || {}), ...Object.values(step.switch_cases || {})];
        }
        return [...new Set(targets.filter((t): t is string => !!t && this.byId.has(t)))];
    }

    private structure() {
        if (this.shape) return this.shape;
        const bodyNext = new Map<string, string[]>();
        const branchNext = new Map<string, [string | null, string]>();
        const members = new Map<string, Set<string>>();
        for (const step of this.byId.values()) {
            if (step.type === 'loop') {
                const body = (step.loop_step_ids || []).filter((b) => this.byId.has(b));
                body.forEach((sid, i) => bodyNext.set(sid, i + 1 < body.length ? [body[i + 1]]
                    : [body[0], ...(step.next_step_id ? [step.next_step_id] : [])]));
            } else if (step.type === 'parallel') {
                const inside = new Set<string>();
                for (const chain of branchChains(step, this.byId)) {
                    const known = chain.filter((c) => this.byId.has(c));
                    known.forEach((sid, i) => {
                        if (!branchNext.has(sid)) branchNext.set(sid, [known[i + 1] ?? null, step.id]);
                        for (const k of contained(sid, this.byId, new Set())) inside.add(k);
                    });
                }
                members.set(step.id, inside);
            }
        }
        this.shape = { bodyNext, branchNext, members };
        return this.shape;
    }
}

export function fieldChecks(steps: StepConfig[], agents?: Agent[], entry?: string | null): FieldCheck[] {
    const all = (steps || []).filter((s): s is AnyStep => !!s && typeof s === 'object') as AnyStep[];
    const writers = new Map<string, AnyStep[]>();
    for (const s of all) for (const key of keysWritten(s)) writers.set(key, [...(writers.get(key) || []), s]);
    // From the entry, each read is judged by the writers a run can bring to it (F1621); without it, by them all.
    const walk = entry ? new Walk(all, entry) : null;
    const knownFor = (sid: string, key: string): AnyStep[] | null => {
        const every = writers.get(key);
        if (!every) return null;
        const ws = walk ? walk.judgedBy(sid, key, every) : every;
        return ws.every((w) => answerFields(w, agents) !== null) ? ws : null;
    };
    const out: FieldCheck[] = [];

    for (const s of all) {
        const known = new Map<string, AnyStep[]>();
        for (const key of writers.keys()) {
            const ws = knownFor(s.id, key);
            if (ws) known.set(key, ws);
        }
        const reads = new Map<string, string[]>();
        const decided = new Set<string>();  // read in a condition or code: missing, it changes the run
        const add = (key: string, field: string) => {
            if (!known.has(key)) return;
            const list = reads.get(key) || [];
            if (!list.includes(field)) reads.set(key, [...list, field]);
        };
        const addDecided = (key: string, field: string) => { add(key, field); decided.add(`${key}.${field}`); };
        for (const f of CONDITIONS) if (typeof s[f] === 'string') codeReads(unbraced(s[f] as string), new Set(known.keys()), addDecided);
        if (typeof s.transform_code === 'string') codeReads(s.transform_code, new Set(known.keys()), addDecided);
        const rest: Record<string, unknown> = {};
        for (const [k, v] of Object.entries(s)) if (k !== 'transform_code' && !CONDITIONS.includes(k)) rest[k] = v;
        templateReads(rest, new Set(known.keys()), add);
        for (const [key, fields] of reads) {
            const ws = known.get(key)!;
            const has = [...new Set(ws.flatMap((w) => answerFields(w, agents) || []))];
            for (const field of fields) {
                if (has.includes(field)) {
                    // Some of the writers a run brings here write it and the rest never do (F1621).
                    const lacking = ws.filter((w) => !(answerFields(w, agents) || []).includes(field));
                    if (lacking.length && walk && decided.has(`${key}.${field}`) && walk.reaching(key).get(s.id)?.size) {
                        const having = ws.filter((w) => !lacking.includes(w));
                        const named = (group: AnyStep[]) => ticked(group.map((w) => w.name || w.id));
                        out.push({
                            stepId: s.id, key, field, kind: 'way',
                            message: `Reads “${field}” from state.${key}, which ${named(having)} write${having.length === 1 ? 's' : ''} ` +
                                `but ${named(lacking)} never write${lacking.length === 1 ? 's' : ''}: on a way through ` +
                                `${named(lacking)} it finds nothing. Give ${named(lacking)} the field too, or read it ` +
                                `only after ${named(having)}.`,
                        });
                    }
                    continue;
                }
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
    // A Loop over a whole answer object, or a field of it that is no list (F1613): `step_answers.loops_over_an_answer`.
    for (const s of all) {
        if (s.type !== 'loop' || typeof s.loop_over !== 'string') continue;
        const m = /^state\.([A-Za-z_]\w*)(?:\.([A-Za-z_]\w*))?$/.exec(unbraced(s.loop_over).trim());
        if (!m || !writers.has(m[1])) continue;
        const [, key, field] = m;
        const ws = walk ? walk.judgedBy(s.id, key, writers.get(key)!) : writers.get(key)!;
        const typed = ws.map(typedAnswer);
        if (typed.some((t) => t === null)) continue;
        let fields: Record<string, string> = Object.assign({}, ...typed);
        let w = ws[0];
        if (walk && field) {
            // Each way's own writer: the first declaring the field as no list is the way the loop fails on (F1621).
            const i = typed.findIndex((t) => !!t && field in t && t[field] !== 'list');
            if (i < 0) continue;
            w = ws[i];
            fields = { ...typed[i] };
        }
        const lists = Object.keys(fields).filter((f) => fields[f] === 'list');
        const name = `“${w.name || w.id}”`;
        const fix = lists.length === 1
            ? `point it at state.${key}.${lists[0]}, its one list`
            : lists.length
                ? `point it at one of its lists: ${lists.map((f) => `state.${key}.${f}`).join(', ')}`
                : 'none of its fields is a list, so a step before the loop makes the list';
        if (!field) {
            out.push({ stepId: s.id, key, field: null, kind: 'list',
                message: `Goes through state.${key}, the whole answer ${name} gives, which is an object, not a list, so ` +
                    `it would run zero times: ${fix}.` });
        } else if (field in fields && fields[field] !== 'list') {
            out.push({ stepId: s.id, key, field, kind: 'list',
                message: `Goes through state.${key}.${field}, which ${name} declares ${fields[field]}, not a list, so ` +
                    `it would run zero times: ${fix}.` });
        }
    }
    return out;
}
