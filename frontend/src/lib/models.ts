/**
 * Model lists, shaped for a picker.
 *
 * `GET /api/models` returns providers, each with its models and whether it is
 * reachable. Two places turn that into `Combobox` options — the Models settings
 * tab and the AI builder's panel — and they were doing it differently: the
 * settings tab grouped by provider and hid unreachable ones, the builder showed
 * one flat list of every model with no headings, which at ~66 entries is a
 * scroll rather than a choice.
 *
 * The provider *labels* live here rather than in the settings tab because both
 * pickers need them. Everything else about how a provider is presented — its
 * icon, its colour, what its API key looks like — stays with the screen that
 * shows those things.
 */

import type { ComboboxOption } from "@/components/ui";
// A cycle by design: providerCards takes its labels from here, and only inside functions, so neither reads the other
// while it loads.
import { providerMark } from "@/lib/providerCards";

export const PROVIDER_LABELS: Record<string, string> = {
    ollama: "Ollama (Local)",
    huggingface: "HuggingFace (Local)",
    gemini: "Google Gemini",
    anthropic: "Anthropic Claude",
    openai: "OpenAI",
    grok: "xAI Grok",
    deepseek: "DeepSeek",
    openai_compatible: "OpenAI Compatible",
    local_compatible: "Local V1 Compatible",
    bedrock: "AWS Bedrock",
    anthropic_cli: "Claude (CLI)",
    gemini_cli: "Gemini (CLI)",
    codex_cli: "Codex (CLI)",
    github_copilot_cli: "GitHub Copilot (CLI)",
    // The hosted OpenAI-style providers (PROV1, D245).
    moonshot: "Moonshot (Kimi)",
    mistral: "Mistral",
    nvidia: "NVIDIA",
    openrouter: "OpenRouter",
    qwen: "Qwen",
    zai: "Z.ai (GLM)",
    groq: "Groq",
    cerebras: "Cerebras",
    together: "Together AI",
    fireworks: "Fireworks",
    minimax: "MiniMax",
};

/**
 * The hosted OpenAI-style providers (PROV1, D245): each is one key, saved as
 * `<id>_key`, and one row on Settings → Models. The backend's table
 * (`core/providers/registry.py`) holds their addresses.
 */
export const HOSTED_PROVIDERS = [
    "moonshot", "mistral", "nvidia", "openrouter", "qwen", "zai", "groq", "cerebras", "together", "fireworks", "minimax",
] as const;
export const HOSTED_KEY_NAMES: string[] = HOSTED_PROVIDERS.map((id) => `${id}_key`);

/** Falls back to the raw key, so a provider added to the backend first still reads. */
export function providerLabel(key: string): string {
    return PROVIDER_LABELS[key] ?? key;
}

/** The shape `/api/models` returns per provider — only the parts a picker needs. */
export interface ProviderModels {
    available: boolean;
    models?: string[];
    embedding_models?: string[];
    /** A model CLI's own word on its sign-in (CLIM1): false signed out, null when it cannot be told without a model call. */
    cli?: { signed_in?: boolean | null } | null;
}

/**
 * Whether a provider's models can be offered: it is reachable, and it is not a model CLI whose own command says it is
 * signed out (F1603: none of a signed-out CLI's models runs). `/api/models` calls an installed CLI available either way,
 * as the engine answers "sign in to Claude Code" from it (F1418); one that cannot be told is offered (CLIM1).
 */
function offered(info: ProviderModels): boolean {
    return info.available && info.cli?.signed_in !== false;
}

/**
 * Grouped by provider, in the order the backend lists them, skipping providers
 * with no key, a model CLI that is signed out (F1603), and providers with nothing
 * to offer. Each option wears its provider's mark, and its group's name is
 * searched with it, so "anthropic" finds Claude's models in every picker built
 * here (F1482).
 *
 * `Combobox` shows its placeholder for a value outside its options, so a picker of
 * a saved model keeps that model with `withUnreachable`.
 */
export function modelOptions(
    providers: Record<string, ProviderModels>,
    kind: "models" | "embedding_models" = "models",
): ComboboxOption[] {
    return Object.entries(providers).flatMap(([key, info]) => {
        const list = info[kind];
        if (!offered(info) || !list?.length) return [];
        const mark = providerMark(key);
        return list.map((model) => ({ value: model, label: model, group: providerLabel(key), mark }));
    });
}

/** The same models as `modelOptions`, as a plain list of ids: for a picker that takes one (a workflow step's, Data Lab's). */
export function offeredModels(providers: Record<string, ProviderModels>): string[] {
    return Object.values(providers).flatMap((info) => (offered(info) ? info.models ?? [] : []));
}

/** A plain list of model ids that keeps a saved model it no longer has, last, so the picker never reads it as blank. */
export function withSaved(models: string[], saved: string | null | undefined): string[] {
    return !saved || models.includes(saved) ? models : [...models, saved];
}

/** What `/api/models` says of each model's window (F1592): a known one's tokens, and the least Synapse AI runs on. */
export interface ModelWindows {
    windows?: Record<string, number>;
    synapse_ai_min_window?: number;
}

/** Where a picker of Synapse AI's model shows a remembered model too small for it, so it never reads as missing. */
export const TOO_SMALL_FOR_SYNAPSE_AI = "Too small for Synapse AI";

/** A window as it is said: 32,768 → "32K", 128,000 → "128K" (a round thousand first: it is also 125 × 1024). */
export function tokensShort(tokens: number): string {
    if (tokens % 1000 === 0) return `${tokens / 1000}K`;
    return `${tokens % 1024 === 0 ? tokens / 1024 : Math.round(tokens / 1000)}K`;
}

/**
 * Any picker of Synapse AI's model (F1592: the panel's, Settings → Models' Builder model, onboarding's Synapse AI's
 * model): `options` but a model known to read less than Synapse AI needs (128K, as cloud's), which would be refused at
 * its first step. A model whose window is not known stays, as the engine never rules one out. A remembered pick that is
 * too small is kept visible under its own group, saying what it reads and what Synapse AI needs, as `withUnreachable`
 * keeps one whose key went.
 */
export function synapseAIOnly(options: ComboboxOption[], said: ModelWindows, current?: string): ComboboxOption[] {
    const { windows = {}, synapse_ai_min_window: floor } = said;
    const small = (id: string) => floor !== undefined && windows[id] !== undefined && windows[id] < floor;
    const fitting = options.filter((o) => !small(o.value));
    if (!current || floor === undefined || !small(current)) return fitting;
    const label = options.find((o) => o.value === current)?.label ?? current;
    return [{ value: current, label, group: TOO_SMALL_FOR_SYNAPSE_AI,
              hint: `reads up to ${tokensShort(windows[current])} tokens; Synapse AI needs ${tokensShort(floor)}` },
            ...fitting];
}

/**
 * A model the workspace is set to but cannot currently reach — its provider has
 * no key, or it is gone from the provider's list.
 *
 * Without this the control would render blank on a value that is really set,
 * which reads as "nothing chosen" and invites overwriting it by accident. The
 * agents screen already solves it this way.
 */
export function withUnreachable(
    options: ComboboxOption[],
    value: string | undefined,
): ComboboxOption[] {
    if (!value || options.some((option) => option.value === value)) return options;
    return [...options, { value, label: value, group: "Not reachable" }];
}
