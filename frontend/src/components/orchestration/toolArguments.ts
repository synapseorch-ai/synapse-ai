/**
 * A Tool step's arguments set on the step (E1, SAI2-P1): the rows the editor shows, and how a typed value is stored.
 *
 * The engine renders each stored value from state (`steps.render_tool_arguments`): `{{state.x}}` standing alone keeps
 * its type, inside text it is written as text. The editor stores what a person types: text stays text where the tool
 * says the argument is text; anywhere else a number, true / false, null or JSON is stored as that value, so `5` reaches
 * a numeric argument as 5, not "5".
 */

/** A tool as the step's picker knows it; `schema` is its input schema when the host lists one. */
export interface ToolWithSchema {
    name: string;
    description: string;
    schema?: { properties?: Record<string, { type?: string | string[]; description?: string }>; required?: string[] } | null;
}

export interface ArgumentRow {
    name: string;
    /** The schema's type for it, or undefined when the tool's schema does not name it. */
    type?: string;
    description?: string;
    required: boolean;
    /** In the tool's schema; a row that is not can be removed. */
    declared: boolean;
    /** As a person reads and types it. */
    text: string;
    set: boolean;
}

function typeOf(raw: string | string[] | undefined): string | undefined {
    if (Array.isArray(raw)) return raw.find((t) => t !== 'null');
    return raw;
}

/** Shown as typed: text as it is, anything else as JSON. */
export function argumentText(value: unknown): string {
    if (value === undefined) return '';
    return typeof value === 'string' ? value : JSON.stringify(value);
}

const LOOKS_TYPED = /^(-?\d+(\.\d+)?([eE][+-]?\d+)?|true|false|null|\{[\s\S]*\}|\[[\s\S]*\])$/;

/** What a typed value is stored as, for an argument of this schema type. */
export function argumentValue(text: string, type: string | undefined): unknown {
    const trimmed = text.trim();
    if (type === 'string' || !LOOKS_TYPED.test(trimmed)) return text;
    try {
        return JSON.parse(trimmed);
    } catch {
        return text;
    }
}

/** The rows: the tool's own arguments (required first), then any other the step already sets. */
export function argumentRows(tool: ToolWithSchema | undefined, set: Record<string, unknown>): ArgumentRow[] {
    const props = tool?.schema?.properties ?? {};
    const required = new Set(tool?.schema?.required ?? []);
    const declared = Object.keys(props).sort((a, b) => Number(required.has(b)) - Number(required.has(a)));
    const rows: ArgumentRow[] = declared.map((name) => ({
        name,
        type: typeOf(props[name]?.type),
        description: props[name]?.description,
        required: required.has(name),
        declared: true,
        text: argumentText(set[name]),
        set: name in set,
    }));
    for (const name of Object.keys(set)) {
        if (!(name in props)) {
            rows.push({ name, required: false, declared: false, text: argumentText(set[name]), set: true });
        }
    }
    return rows;
}

/** The step's arguments after one row is typed: an empty value is no argument. */
export function withArgument(set: Record<string, unknown>, row: Pick<ArgumentRow, 'name' | 'type'>, text: string) {
    const next = { ...set };
    if (text.trim() === '') delete next[row.name];
    else next[row.name] = argumentValue(text, row.type);
    return next;
}
