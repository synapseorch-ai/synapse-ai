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

/**
 * What `/api/models` says of each model beside its provider's list: a known window and the least Synapse AI runs on
 * (F1592); and its fit for building, its price in USD per 1M tokens and the engine's order (F1598). Each is only what
 * is known: a model it says nothing of is left out of each.
 */
export interface ModelWindows {
    windows?: Record<string, number>;
    synapse_ai_min_window?: number;
    builder_fit?: Record<string, "recommended" | "not_for_building">;
    prices?: Record<string, { in: number; out: number }>;
    ranked?: string[];
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
 *
 * Where the server says each model's fit (F1598): the models recommended for building first, each tagged
 * "Recommended", then "Other models", unlabelled, each in the engine's order; a model not for building is left out, and
 * a remembered one kept visible saying why. Never empty: with nothing rated, every model that fits is listed. Without a
 * fit said (an older server), the groups are as they were.
 */
export function synapseAIOnly(options: ComboboxOption[], said: ModelWindows, current?: string): ComboboxOption[] {
    const { windows = {}, synapse_ai_min_window: floor, builder_fit: fits } = said;
    const small = (id: string) => floor !== undefined && windows[id] !== undefined && windows[id] < floor;
    const left = (id: string) => fits?.[id] === "not_for_building";
    const fitting = options.filter((o) => !small(o.value) && !left(o.value));
    const listed = fits ? rated(fitting, fits, said.ranked) : fitting;
    if (!current) return listed;
    const label = options.find((o) => o.value === current)?.label ?? current;
    const why = synapseAIDefaultHint(current, said);
    if (why === undefined) return listed;
    return [{ value: current, label, group: small(current) ? TOO_SMALL_FOR_SYNAPSE_AI : NOT_FOR_BUILDING, hint: why },
            ...listed];
}

/** What a saved model left out of Synapse AI's pickers says, on its own row and on a "Default (…)" row that runs on it. */
const NOT_OFFERED_FOR_BUILDING = "Not offered for building with Synapse AI: pick a Recommended model";

/**
 * Why Synapse AI's pickers leave `model` out, in the words its row says (F1592, F1598): what it reads, when that is
 * less than Synapse AI needs; else that it is not offered for building. A "Default (…)" or "Same as … model" row that
 * runs Synapse AI on it says the same, so it never quietly offers what the list leaves out. Undefined for a model that
 * is not left out, or not known.
 */
export function synapseAIDefaultHint(model: string | null | undefined, said: ModelWindows): string | undefined {
    if (!model) return undefined;
    const { windows = {}, synapse_ai_min_window: floor } = said;
    if (floor !== undefined && windows[model] !== undefined && windows[model] < floor) {
        return `reads up to ${tokensShort(windows[model])} tokens; Synapse AI needs ${tokensShort(floor)}`;
    }
    return said.builder_fit?.[model] === "not_for_building" ? NOT_OFFERED_FOR_BUILDING : undefined;
}

/** The note at the foot of Synapse AI's pickers (F1598): what they leave out, and when nothing listed is rated for
 *  building yet. A server that says no fit (an older one) rates nothing, so the second part is not said. */
export function synapseAINote(options: ComboboxOption[], said?: ModelWindows): string {
    if (said && !said.builder_fit) return SYNAPSE_AI_NOTE;
    return options.some((o) => o.group === RECOMMENDED_FOR_BUILDING) ? SYNAPSE_AI_NOTE : `${SYNAPSE_AI_NOTE} ${NONE_RATED}`;
}

/** A window as a picker's line says it: its size, small under 128K, standard from 128K, large from 500K (F1598). */
function contextWords(tokens: number): string {
    const size = tokens >= 1_000_000 ? `${Number((tokens / 1_000_000).toFixed(1))}M` : tokensShort(tokens);
    const band = tokens < 128_000 ? "Small" : tokens < 500_000 ? "Standard" : "Large";
    return `${band} context (${size})`;
}

/** The engine's own price bands, USD per 1M output tokens (`models_menu.STAND_IN_BANDS` and its ceiling, past which
 *  it never picks on its own): a cost tier from `$` to `$$$$`. */
const COST_BANDS: [number, string][] = [[1, "$"], [5, "$$"], [15, "$$$"]];

function dollars(price: number): string {
    return price >= 0.1 || price === 0 ? price.toFixed(2) : String(Number(price.toPrecision(2)));
}

/**
 * The line under a model in every picker (F1598): its context, its cost tier and its price, e.g. "Large context (1M)
 * · $$$ · $3.00 in / $15.00 out per 1M tokens". What is not known is left out, never guessed: "" when nothing is.
 */
export function modelHint(model: string, said: ModelWindows): string {
    const parts: string[] = [];
    const tokens = said.windows?.[model];
    if (tokens) parts.push(contextWords(tokens));
    const price = said.prices?.[model];
    if (price && typeof price.in === "number" && typeof price.out === "number") {
        parts.push(COST_BANDS.find(([upTo]) => price.out <= upTo)?.[1] ?? "$$$$");
        parts.push(`$${dollars(price.in)} in / $${dollars(price.out)} out per 1M tokens`);
    }
    return parts.join(" · ");
}

/** `options` each with its model's line (`modelHint`), where something is known of it and it has no hint of its own. */
export function withHints(options: ComboboxOption[], said: ModelWindows | undefined): ComboboxOption[] {
    if (!said) return options;
    return options.map((option) => {
        if (option.hint) return option;
        const hint = modelHint(option.value, said);
        return hint ? { ...option, hint } : option;
    });
}

/** Each model's line by id, for a picker of plain ids (a step's model, Data Lab's): only the ones with something said. */
export function modelHints(models: string[], said: ModelWindows | undefined): Record<string, string> {
    if (!said) return {};
    return Object.fromEntries(models.map((m) => [m, modelHint(m, said)] as const).filter(([, hint]) => hint));
}

/** Where Synapse AI's pickers list the models recommended for building, then the rest, and a saved one left out. */
export const RECOMMENDED_FOR_BUILDING = "Recommended for building";
export const OTHER_MODELS = "Other models";
export const NOT_FOR_BUILDING = "Not for building";
const RECOMMENDED: { text: string; tone: "success" } = { text: "Recommended", tone: "success" };
/** What Synapse AI's pickers say at the foot of their list (F1598). */
export const SYNAPSE_AI_NOTE = "Some models aren't offered here, such as ones too small for what Synapse AI reads.";
const NONE_RATED = "None of these is rated for building yet.";

/** Recommended first, then the rest, each in the engine's order (`ranked`); a model it did not rank keeps its place
 *  after the ranked ones. */
function rated(options: ComboboxOption[], fits: Record<string, string>, ranked: string[] = []): ComboboxOption[] {
    const place = (id: string) => (ranked.includes(id) ? ranked.indexOf(id) : ranked.length);
    const ordered = options.map((option, n) => ({ option, n }))
        .sort((a, b) => place(a.option.value) - place(b.option.value) || a.n - b.n)
        .map(({ option }) => option);
    return [
        ...ordered.filter((o) => fits[o.value] === "recommended")
            .map((o) => ({ ...o, group: RECOMMENDED_FOR_BUILDING, tag: RECOMMENDED })),
        ...ordered.filter((o) => fits[o.value] !== "recommended").map((o) => ({ ...o, group: OTHER_MODELS })),
    ];
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
