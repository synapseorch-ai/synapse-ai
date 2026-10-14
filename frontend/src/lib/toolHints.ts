/**
 * What a custom HTTP tool does, as the tool editor offers it (F488, BWA1).
 *
 * The backend gives a saved tool the MCP hints its method implies
 * (`core.tools.method_hints`) unless the tool carries its own; this is the same
 * table, so the editor shows what a save will store and the person can say
 * otherwise. The tool policy reads them: under `open_world: confirm` or
 * `destructive: confirm`, a tool that changes or deletes something waits for a
 * person first.
 */

export type ToolEffect = 'reads' | 'changes' | 'deletes';

export type ToolAnnotations = {
    readOnlyHint?: boolean;
    destructiveHint?: boolean;
    idempotentHint?: boolean;
    openWorldHint?: boolean;
};

export const TOOL_EFFECTS: { value: ToolEffect; label: string }[] = [
    { value: 'reads', label: 'Reads only' },
    { value: 'changes', label: 'Changes something' },
    { value: 'deletes', label: 'Deletes something' },
];

export function annotationsFor(effect: ToolEffect): ToolAnnotations {
    if (effect === 'reads') return { readOnlyHint: true, idempotentHint: true, openWorldHint: true };
    if (effect === 'deletes') return { readOnlyHint: false, destructiveHint: true, openWorldHint: true };
    return { readOnlyHint: false, destructiveHint: false, openWorldHint: true };
}

/** What a method implies, as the backend's `method_hints` does: GET reads, DELETE deletes, the rest change. */
export function effectOfMethod(method: string | undefined): ToolEffect {
    const m = (method || 'GET').toUpperCase();
    if (m === 'GET' || m === 'HEAD' || m === 'OPTIONS') return 'reads';
    if (m === 'DELETE') return 'deletes';
    return 'changes';
}

export function effectOf(annotations: ToolAnnotations | null | undefined, method: string | undefined): ToolEffect {
    if (!annotations) return effectOfMethod(method);
    if (annotations.readOnlyHint) return 'reads';
    return annotations.destructiveHint ? 'deletes' : 'changes';
}
