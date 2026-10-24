/**
 * What a custom tool does, as the tool editor offers it (F488, BWA1; TAG1, D296).
 *
 * Four tags, MCP's tool annotations by our names: read-only, destructive, safe to repeat (idempotent) and reaches
 * outside (open-world). The backend gives a saved HTTP tool its method's tags (`core/tool_tags.py`) unless the
 * person set their own; `METHOD_TAGS` here is the same table (`test_tool_tags_agree` holds the two together), so the
 * editor shows what a save will store and the person can say otherwise. The tool policy reads them: under
 * `open_world: confirm` or `destructive: confirm` a tool that changes or deletes something waits for a person first,
 * and a workflow does not send a call again after a timeout unless the tool is safe to repeat.
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
    // `deletes` is MCP's destructive: a change that can't be undone, a sent email as much as a removed row (F877).
    { value: 'deletes', label: 'Changes something for good' },
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
    return effectOfTags(tagsOf(annotations)!);
}

// ── The four tags (TAG1) ─────────────────────────────────────────────────────

export type ToolTag = 'read_only' | 'destructive' | 'idempotent' | 'open_world';
export type ToolTags = Record<ToolTag, boolean>;

const READ: ToolTags = { read_only: true, destructive: false, idempotent: true, open_world: true };
const WRITE: ToolTags = { read_only: false, destructive: false, idempotent: false, open_world: true };

/** What an HTTP method implies (RFC 9110: GET, HEAD, OPTIONS read; PUT and DELETE are safe to repeat). Every HTTP
 *  tool reaches outside until the person unticks it (Naveen, 2026-10-17). */
export const METHOD_TAGS: Record<string, ToolTags> = {
    GET: READ, HEAD: READ, OPTIONS: READ,
    PUT: { ...WRITE, idempotent: true },
    POST: WRITE,
    PATCH: WRITE,
    DELETE: { ...WRITE, destructive: true, idempotent: true },
};

export function defaultsFor(method: string | undefined): ToolTags {
    return { ...(METHOD_TAGS[(method || 'GET').toUpperCase()] ?? WRITE) };
}

/** The tags stored hints say, or null when there are none ("not set"). The engine's rule (`core.tool_tags`): a tool
 *  that is not read-only and does not say `destructiveHint: false` is taken as destructive, as MCP's default is. */
export function tagsOf(annotations: ToolAnnotations | null | undefined): ToolTags | null {
    if (!annotations || typeof annotations !== 'object') return null;
    const readOnly = !!annotations.readOnlyHint;
    return {
        read_only: readOnly,
        destructive: !readOnly && annotations.destructiveHint !== false,
        idempotent: !!annotations.idempotentHint,
        open_world: !!annotations.openWorldHint,
    };
}

/** MCP's hints for `tags`, all four for a write; a read-only tool carries no destructiveHint. */
export function hintsOf(tags: ToolTags): ToolAnnotations {
    const out: ToolAnnotations = {
        readOnlyHint: tags.read_only, destructiveHint: tags.destructive,
        idempotentHint: tags.idempotent, openWorldHint: tags.open_world,
    };
    if (tags.read_only) delete out.destructiveHint;
    return out;
}

export function effectOfTags(tags: ToolTags): ToolEffect {
    if (tags.read_only) return 'reads';
    return tags.destructive ? 'deletes' : 'changes';
}

/** `tags` with "What it does" set to `effect`, keeping "reaches outside" and, for a write, "safe to repeat". A tool
 *  that only reads may always be asked again. */
export function withEffect(tags: ToolTags, effect: ToolEffect): ToolTags {
    if (effect === 'reads') return { ...tags, read_only: true, destructive: false, idempotent: true };
    return { ...tags, read_only: false, destructive: effect === 'deletes',
        idempotent: tags.read_only ? false : tags.idempotent };
}

export function sameTags(a: ToolTags | null, b: ToolTags | null): boolean {
    if (!a || !b) return a === b;
    return (Object.keys(a) as ToolTag[]).every(k => a[k] === b[k]);
}

/** The short chips a tool list shows: what it does, "Can't undo" for a destructive write, then "Safe to repeat" for a
 *  write and "Reaches outside". An untrusted "reads" (an outside server's, the AI builder's) is shown as the claim it
 *  is: the policy doesn't believe it. Destructive is MCP's "can't be undone", not "deletes": gmail_send carries it, and
 *  the tags cannot tell a send from a delete, so no chip says which (F877; Naveen 2026-10-24). */
export function tagChips(tags: ToolTags | null, trusted = true): string[] {
    if (!tags) return ['Not set'];
    const chips = [tags.read_only ? (trusted ? 'Reads' : 'Says it only reads') : 'Changes'];
    if (!tags.read_only && tags.destructive) chips.push("Can't undo");
    if (!tags.read_only && tags.idempotent) chips.push('Safe to repeat');
    if (tags.open_world) chips.push('Reaches outside');
    return chips;
}

/** Whether a URL plainly points inside the person's own network (localhost, a private address, a `.local` or
 *  `.internal` name, or a one-word host such as a Docker service). Only for the editor's note: n8n at localhost
 *  still sends the email, so the tag stays the person's to untick. A templated host is not judged. */
export function isLocalUrl(url: string | undefined): boolean {
    const raw = (url || '').trim();
    if (!raw || raw.includes('{{')) return false;
    let host: string;
    try {
        host = new URL(/^[a-z]+:\/\//i.test(raw) ? raw : `http://${raw}`).hostname.toLowerCase();
    } catch {
        return false;
    }
    host = host.replace(/^\[|\]$/g, '');
    if (host === 'localhost' || host.endsWith('.localhost') || host === '::1') return true;
    if (host.endsWith('.local') || host.endsWith('.internal') || host.endsWith('.lan')) return true;
    const ip = host.match(/^(\d+)\.(\d+)\.\d+\.\d+$/);
    if (ip) {
        const [a, b] = [Number(ip[1]), Number(ip[2])];
        return a === 127 || a === 10 || (a === 192 && b === 168) || (a === 172 && b >= 16 && b <= 31)
            || (a === 169 && b === 254) || a === 0;
    }
    if (/^(fc|fd)[0-9a-f]{2}:/.test(host) || host.startsWith('fe80:')) return true;
    return !host.includes('.') && !host.includes(':');
}
