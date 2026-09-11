/**
 * The tool-policy control, shared by the agent editor and Settings → General.
 *
 * The engine decides a tool call by what the tool *does* rather than by its
 * name (`core/tool_policy.py`): a declared-destructive tool can be held for a
 * person or refused outright without taking it away from the agent. Until this
 * screen existed the whole feature was reachable only by hand-editing an agent
 * document — and the API dropped the field on the next save, so even that did
 * not survive.
 *
 * `resolvePolicy` and `gatingCategories` mirror the engine functions of the
 * same name. They are here so the editor can say what a choice will do before
 * it is saved; the engine remains the only thing that enforces any of it.
 */
import { Label } from '@/components/ui';

export type ToolPolicy = Record<string, string>;

/** Riskiest first — the first category a tool matches decides. */
export const POLICY_CATEGORIES = [
    {
        id: 'destructive',
        label: 'Tools that change data permanently',
        // Mid-sentence, where the plural heading does not fit. Kept close to
        // `core/tool_policy.py`'s own REASONS so a person reading the refusal
        // and a person reading this screen hear the same thing.
        clause: 'a tool that can change data permanently',
        hint: 'The tool declares that it modifies data in a way that cannot be undone.',
    },
    {
        id: 'open_world',
        label: 'Tools that reach outside this workspace',
        clause: 'a tool that reaches outside this workspace',
        hint: 'The tool declares that it talks to systems this workspace does not control.',
    },
    {
        id: 'undeclared',
        label: "Tools that don't say what they do",
        clause: "a tool that doesn't say what it does",
        hint: 'Most tools. An outside MCP server’s promise that its tool is safe is a claim rather than a boundary, so a read-only tool from one lands here too.',
    },
] as const;

export const POLICY_CHOICES = [
    { value: 'allow', label: 'Allow' },
    { value: 'confirm', label: 'Ask first' },
    { value: 'deny', label: 'Block' },
] as const;

/** What each decision means, in one line, under the control. */
export const POLICY_MEANING: Record<string, string> = {
    allow: 'The call runs.',
    confirm: 'The run stops and a person decides. In chat that is a card in the conversation; in an orchestration the run waits for input.',
    deny: 'The call never runs, and the model is told why.',
};

/** Built-in defaults, then the workspace’s, then the agent’s — the order in
 *  `core/tool_policy.py::resolve_policy`. Unknown keys and values are ignored
 *  rather than obeyed, here as there. */
export function resolvePolicy(agent: ToolPolicy | null | undefined, defaults: ToolPolicy | null | undefined): ToolPolicy {
    const valid = new Set(POLICY_CHOICES.map(c => c.value as string));
    const out: ToolPolicy = {};
    for (const c of POLICY_CATEGORIES) out[c.id] = 'allow';
    for (const source of [defaults, agent]) {
        if (!source || typeof source !== 'object') continue;
        for (const c of POLICY_CATEGORIES) {
            if (valid.has(source[c.id])) out[c.id] = source[c.id];
        }
    }
    return out;
}

/** The categories that would stop and ask a person — the set a chat widget
 *  refuses to publish, because a visitor is not a member and cannot answer. */
export function gatingCategories(agent: ToolPolicy | null | undefined, defaults: ToolPolicy | null | undefined): string[] {
    const resolved = resolvePolicy(agent, defaults);
    return POLICY_CATEGORIES.filter(c => resolved[c.id] === 'confirm').map(c => c.id);
}

interface ToolPolicyFieldsProps {
    value: ToolPolicy;
    onChange: (next: ToolPolicy) => void;
    /** The workspace defaults an unset category falls back to. Omit at the
     *  workspace level, where there is nothing above to inherit and unset
     *  simply means Allow. */
    inheritFrom?: ToolPolicy | null;
    disabled?: boolean;
}

export function ToolPolicyFields({ value, onChange, inheritFrom, disabled = false }: ToolPolicyFieldsProps) {
    const canInherit = inheritFrom !== undefined && inheritFrom !== null;
    const resolvedDefaults = resolvePolicy(null, inheritFrom);

    const set = (category: string, decision: string | null) => {
        const next = { ...value };
        if (decision === null) delete next[category];
        else next[category] = decision;
        onChange(next);
    };

    return (
        <div className="space-y-3">
            {POLICY_CATEGORIES.map(cat => {
                const chosen = value?.[cat.id];
                const effective = chosen ?? (canInherit ? resolvedDefaults[cat.id] : 'allow');
                return (
                    <div key={cat.id} className="space-y-1">
                        <Label className="block">{cat.label}</Label>
                        <div className="flex gap-1">
                            {canInherit && (
                                <button
                                    type="button"
                                    disabled={disabled}
                                    onClick={() => set(cat.id, null)}
                                    aria-pressed={chosen === undefined}
                                    className={`flex-1 border p-2 text-2xs uppercase tracking-wide rounded-md transition-colors disabled:opacity-50 ${
                                        chosen === undefined
                                            ? 'border-accent bg-accent/10 text-accent'
                                            : 'border-border bg-bg text-text-muted hover:border-border-strong hover:text-text'
                                    }`}
                                >
                                    Default
                                </button>
                            )}
                            {POLICY_CHOICES.map(choice => {
                                const active = chosen === choice.value;
                                return (
                                    <button
                                        key={choice.value}
                                        type="button"
                                        disabled={disabled}
                                        onClick={() => set(cat.id, choice.value)}
                                        aria-pressed={active}
                                        className={`flex-1 border p-2 text-2xs uppercase tracking-wide rounded-md transition-colors disabled:opacity-50 ${
                                            active
                                                ? 'border-accent bg-accent/10 text-accent'
                                                : 'border-border bg-bg text-text-muted hover:border-border-strong hover:text-text'
                                        }`}
                                    >
                                        {choice.label}
                                    </button>
                                );
                            })}
                        </div>
                        <p className="text-2xs text-text-faint mt-1">{cat.hint}</p>
                        <p className="text-2xs text-text-muted">
                            {chosen === undefined && canInherit && (
                                <span className="text-text-faint">
                                    Following the workspace setting ({POLICY_CHOICES.find(c => c.value === effective)?.label}) —{' '}
                                </span>
                            )}
                            {POLICY_MEANING[effective]}
                        </p>
                    </div>
                );
            })}
        </div>
    );
}
