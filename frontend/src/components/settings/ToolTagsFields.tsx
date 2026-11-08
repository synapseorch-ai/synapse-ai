/**
 * What a custom tool says it does (F488; TAG1, D296): "What it does", "Safe to repeat" and "Reaches outside this
 * workspace", MCP's four tool annotations by name.
 *
 * An HTTP tool starts from its method's tags (`lib/toolHints.ts` METHOD_TAGS, the engine's table) and the person can
 * say otherwise; a Python tool starts not set, which the tool policy treats as a tool that does not say what it does.
 * The tool policy reads them, and a workflow does not send a call again after a timeout unless the tool is safe to
 * repeat.
 */
import { Button, Checkbox, Label, Select } from '@/components/ui';
import {
    defaultsFor, effectOfTags, hintsOf, isLocalUrl, sameTags, tagsOf, TOOL_EFFECTS, withEffect,
    type ToolAnnotations, type ToolEffect, type ToolTags,
} from '@/lib/toolHints';

function hostOf(url: string | undefined): string {
    const raw = (url || '').trim();
    if (!raw || raw.includes('{{')) return '';
    try {
        return new URL(/^[a-z]+:\/\//i.test(raw) ? raw : `https://${raw}`).host;
    } catch {
        return '';
    }
}

export function ToolTagsFields({ kind, method, url, annotations, onChange, disabled = false }: {
    kind: 'http' | 'python';
    method?: string;
    url?: string;
    /** The draft's hints: undefined for an HTTP tool that takes its method's on save; null or undefined for a Python
     *  tool that has none. */
    annotations: ToolAnnotations | null | undefined;
    /** undefined: an HTTP tool takes its method's tags again; null: a Python tool is not set again. */
    onChange: (next: ToolAnnotations | null | undefined) => void;
    /** A person who may not edit the tool sees its tags, unchangeable. */
    disabled?: boolean;
}) {
    const stored = tagsOf(annotations);
    if (kind === 'python' && !stored) {
        return (
            <div className="space-y-2" data-tool-tags="not-set">
                <Label className="block">What it does</Label>
                <p className="text-xs text-text-muted">
                    Not set. Your tool policy treats it as a tool that doesn’t say what it does.
                </p>
                <Button type="button" variant="secondary" size="sm" disabled={disabled}
                    onClick={() => onChange(hintsOf(defaultsFor('POST')))}>
                    Say what it does
                </Button>
            </div>
        );
    }

    const fromMethod = defaultsFor(method || 'POST');
    const tags: ToolTags = stored ?? fromMethod;
    const set = (next: ToolTags) => onChange(hintsOf(next));
    const follows = kind === 'http' && (!stored || sameTags(stored, fromMethod));
    const reads = tags.read_only;
    const where = [(method || 'POST').toUpperCase(), hostOf(url)].filter(Boolean).join(' ');

    return (
        <div className="space-y-2" data-tool-tags={follows ? 'from-method' : 'own'}>
            <Label className="block">What it does</Label>
            <Select
                value={effectOfTags(tags)}
                onChange={v => set(withEffect(tags, v as ToolEffect))}
                aria-label="What the tool does"
                options={TOOL_EFFECTS}
                disabled={disabled}
            />
            <div className="grid gap-2 pt-1">
                <div className="flex items-start gap-2">
                    <Checkbox
                        id="tool-tag-idempotent"
                        checked={reads || tags.idempotent}
                        disabled={disabled || reads}
                        onChange={c => set({ ...tags, idempotent: c })}
                        label="Safe to repeat"
                    />
                    <div className="grid gap-0.5">
                        <label htmlFor="tool-tag-idempotent" className="text-sm text-text">Safe to repeat</label>
                        <span className="text-xs text-text-muted">
                            {reads
                                ? 'A tool that only reads can always be asked again.'
                                : 'Calling it twice changes nothing the first call didn’t. If it isn’t, a workflow won’t send it again after a timeout.'}
                        </span>
                    </div>
                </div>
                <div className="flex items-start gap-2">
                    <Checkbox
                        id="tool-tag-open-world"
                        checked={tags.open_world}
                        disabled={disabled}
                        onChange={c => set({ ...tags, open_world: c })}
                        label="Reaches outside this workspace"
                    />
                    <div className="grid gap-0.5">
                        <label htmlFor="tool-tag-open-world" className="text-sm text-text">Reaches outside this workspace</label>
                        <span className="text-xs text-text-muted">It talks to a system this workspace doesn’t control.</span>
                    </div>
                </div>
            </div>
            {kind === 'http' && tags.open_world && isLocalUrl(url) && (
                <p className="text-xs text-text-muted" data-tool-tags-local="">
                    This address is inside your network. Untick “Reaches outside” if what it calls goes no further.
                </p>
            )}
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                <p className="text-xs text-text-muted">
                    {follows
                        ? `Set from ${where}. Change them if that’s wrong.`
                        : 'Your tool policy reads these: a tool that changes or deletes something can wait for a person before it runs.'}
                </p>
                {kind === 'http' && !follows && !disabled && (
                    <Button type="button" variant="ghost" size="sm" onClick={() => onChange(undefined)}>
                        Use the method’s
                    </Button>
                )}
                {kind === 'python' && !disabled && (
                    <Button type="button" variant="ghost" size="sm" onClick={() => onChange(null)}>
                        Clear
                    </Button>
                )}
            </div>
        </div>
    );
}
