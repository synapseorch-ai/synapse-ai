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
};

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
 * with no key and providers with nothing to offer.
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
        return list.map((model) => ({ value: model, label: model, group: providerLabel(key) }));
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
