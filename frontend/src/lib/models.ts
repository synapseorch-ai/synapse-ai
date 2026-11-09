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
}

/**
 * Grouped by provider, in the order the backend lists them, skipping providers
 * with no key and providers with nothing to offer. Each option wears its
 * provider's mark, and its group's name is searched with it, so "anthropic"
 * finds Claude's models in every picker built here (PICK1, F1482).
 *
 * `Combobox` does its own filtering and does not drop the selected value out
 * from under itself, so nothing here has to guard the current selection.
 */
export function modelOptions(
    providers: Record<string, ProviderModels>,
    kind: "models" | "embedding_models" = "models",
): ComboboxOption[] {
    return Object.entries(providers).flatMap(([key, info]) => {
        const list = info[kind];
        if (!info.available || !list?.length) return [];
        const mark = providerMark(key);
        return list.map((model) => ({ value: model, label: model, group: providerLabel(key), mark }));
    });
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
