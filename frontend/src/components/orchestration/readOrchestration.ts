/**
 * A saved workflow as the editor reads it: each field in the shape the editor draws (F1054; HOT2, D323).
 *
 * The editor trusted the stored document. A human step saved with `human_fields: {"fields": [...]}` (rc4.3's builder,
 * prod 10-24) threw inside the canvas's checks, and with nothing to catch it the whole page went to the error card.
 * Every open goes through here first: a field in another shape is read into the editor's, and each step says what was
 * read, as a warning, until a save writes it back in that shape. An object holding just one list is that list (the
 * builder's wrapping); anything else that cannot be read is left empty, never guessed at. Pure: the stored document is
 * not changed, so nothing is lost until the person saves.
 */

import { STEP_TYPE_META, type Orchestration, type StepConfig } from '@/types/orchestration';

import { namedHumanFields } from './humanFields';

/** What was read differently, by step id; `''` is the workflow itself. */
export type ShapeNotes = Record<string, string[]>;

const LISTS = ['input_keys', 'human_fields', 'parallel_branches', 'loop_step_ids', 'finish_tools', 'allowed_tools'] as const;
const OBJECTS = ['route_map', 'route_descriptions', 'switch_cases', 'tool_arguments', 'finish_fallback', 'retrieve', 'ingest'] as const;
const TEXTS = [
    'name', 'agent_id', 'prompt_template', 'evaluator_prompt', 'model', 'merge_strategy', 'loop_over', 'loop_item_key',
    'loop_index_key', 'loop_while_condition', 'loop_break_condition', 'forced_tool', 'workflow_id', 'transform_code',
    'print_content', 'if_condition', 'if_true_step_id', 'if_false_step_id', 'switch_expression', 'switch_default_step_id',
    'human_prompt', 'human_channel_id', 'output_key', 'next_step_id', 'on_exhausted_step_id',
] as const;

/** A field as the step's card names it; the issue list is plain text, so no field names in code. */
const WORDS: Record<string, string> = {
    human_fields: 'form fields', input_keys: 'input keys', parallel_branches: 'branches', loop_step_ids: 'loop steps',
    finish_tools: 'finishing tools', allowed_tools: 'allowed tools', route_map: 'routes', route_descriptions: 'route descriptions',
    switch_cases: 'cases', tool_arguments: 'tool arguments', finish_fallback: 'fallback output',
    retrieve: 'knowledge search settings', ingest: 'knowledge settings',
};
const words = (field: string) => WORDS[field] ?? field.replace(/_/g, ' ');

const isObject = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null && !Array.isArray(v);

function shapeOf(v: unknown): string {
    if (Array.isArray(v)) return 'a list';
    if (isObject(v)) return 'an object';
    return typeof v === 'string' ? 'text' : typeof v === 'number' ? 'a number' : typeof v === 'boolean' ? 'true/false' : String(v);
}

/** One list field: the list, the one list an object holds, or empty. */
function readList(field: string, v: unknown, say: (s: string) => void): unknown[] {
    let list: unknown[];
    if (Array.isArray(v)) {
        list = v;
    } else {
        const held = isObject(v) ? Object.values(v) : [];
        if (held.length === 1 && Array.isArray(held[0])) {
            list = held[0];
            say(`Its ${words(field)} were saved inside an object; they are shown as the list it holds. Save to keep them that way.`);
        } else {
            say(`Its ${words(field)} were saved as ${shapeOf(v)}, not a list, so they are left empty. Set them again.`);
            return [];
        }
    }
    // The entries in their shape too: a field is an object, a branch a list of ids, a key or tool a name.
    if (field === 'human_fields') {
        const kept = list.filter(isObject);
        if (kept.length < list.length) say('A form field the editor could not read was left out. Save to drop it, or set it again.');
        // A field named by `key`, as the template packs wrote one, is named by it (F1557): the run reads it so too.
        const named = namedHumanFields(kept);
        if (named.some((f, i) => f !== kept[i])) {
            say('Its form fields were named by “key”, as the template packs wrote them; each is shown by that name. Save to keep them so.');
        }
        return named;
    }
    if (field === 'parallel_branches') {
        return list.flatMap((b) => (Array.isArray(b) ? [b.filter((id) => typeof id === 'string')] : typeof b === 'string' ? [[b]] : []));
    }
    return list.filter((x) => typeof x === 'string');
}

/** One step as the editor reads it, and what was read differently. */
function readStep(raw: Record<string, unknown>, index: number, notes: ShapeNotes): StepConfig {
    const step: Record<string, unknown> = { ...raw };
    if (typeof step.id !== 'string' || !step.id) step.id = `step_${index + 1}`;
    const say = (s: string) => { (notes[step.id as string] ??= []).push(s); };
    for (const field of LISTS) {
        const v = step[field];
        if (v === undefined || v === null) continue;
        step[field] = readList(field, v, say);
    }
    for (const field of OBJECTS) {
        const v = step[field];
        if (v === undefined || v === null || isObject(v)) continue;
        delete step[field];
        say(`Its ${words(field)} were saved as ${shapeOf(v)}, which the editor cannot read, so they are left unset. Set them again.`);
    }
    for (const field of TEXTS) {
        const v = step[field];
        if (v === undefined || v === null || typeof v === 'string') continue;
        if (typeof v === 'number' || typeof v === 'boolean') {
            step[field] = String(v);
            continue;
        }
        delete step[field];
        say(`Its ${words(field)} was saved as ${shapeOf(v)}, not text, so it is left unset. Set it again.`);
    }
    if (typeof step.name !== 'string') step.name = step.id as string;
    if (!(String(step.type) in STEP_TYPE_META)) say(`Its type, "${String(step.type)}", is not one this editor knows, so the step is not drawn.`);
    return step as unknown as StepConfig;
}

/** The workflow as the editor reads it: a copy, the stored document untouched. */
export function readOrchestration<T extends Orchestration | null | undefined>(raw: T): { orchestration: T; notes: ShapeNotes } {
    const notes: ShapeNotes = {};
    if (!raw) return { orchestration: raw, notes };
    const doc = raw as unknown as Record<string, unknown>;
    const global = (s: string) => { (notes[''] ??= []).push(s); };
    let steps: StepConfig[];
    if (Array.isArray(doc.steps)) {
        steps = doc.steps.flatMap((s, i) => (isObject(s) ? [readStep(s, i, notes)] : []));
        if (steps.length < doc.steps.length) global('A step the editor could not read was left out.');
    } else {
        steps = [];
        if (doc.steps !== undefined && doc.steps !== null) global(`The workflow's steps were saved as ${shapeOf(doc.steps)}, not a list, so none are shown.`);
    }
    let schema = doc.state_schema;
    if (schema !== undefined && schema !== null && !isObject(schema)) {
        global(`The workflow's state keys were saved as ${shapeOf(schema)}, which the editor cannot read, so none are shown.`);
        schema = {};
    } else if (isObject(schema) && Object.values(schema).some((e) => !isObject(e))) {
        schema = Object.fromEntries(Object.entries(schema).filter(([, e]) => isObject(e)));
        global('A state key the editor could not read was left out.');
    }
    return { orchestration: { ...doc, steps, state_schema: schema ?? {} } as unknown as T, notes };
}
