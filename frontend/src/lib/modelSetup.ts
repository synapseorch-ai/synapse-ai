/**
 * The first-run model step's logic (ONB2, F728, D303): the open-source twin of
 * cloud's `lib/onboardingModels.ts`, without cloud's managed models and prices.
 *
 * It reads only what Settings → Models reads: `/api/models` (each provider's
 * models; a model CLI's own word on itself, asked without a model call, CLIM1)
 * and the keys' `<name>_set` from `/api/settings`. Nothing here holds a key.
 */
import type { ComboboxOption } from '@/components/ui';
import { modelOptions } from '@/lib/models';
import { PROVIDER_META, secretOf, type ProviderInfo } from '@/lib/providerCards';
import type { SecretState } from '@/components/settings/SavedSecret';

/** Shown first; the rest behind "More providers" (cloud's order). */
export const FIRST_PROVIDERS = ['openai', 'anthropic', 'gemini', 'grok', 'deepseek', 'mistral'];

/** Providers a key alone connects. Bedrock, a server of your own and HuggingFace need more than a key: Settings → Models. */
const NEEDS_MORE = new Set(['bedrock', 'openai_compatible', 'local_compatible', 'huggingface']);
export const ONBOARDING_PROVIDERS = [
    ...FIRST_PROVIDERS,
    ...Object.keys(PROVIDER_META).filter(k => !FIRST_PROVIDERS.includes(k) && !NEEDS_MORE.has(k) && secretOf(k)),
];

/** The model CLIs, in Settings → Models' order. */
export const CLI_PROVIDERS = ['anthropic_cli', 'gemini_cli', 'codex_cli', 'github_copilot_cli'];

/** The provider whose free key the step suggests to someone with nothing at all (Naveen, 2026-10-20). */
export const FREE_KEY_PROVIDER = 'gemini';

/** Every key name a tile can show as saved. */
export const KEY_NAMES = ONBOARDING_PROVIDERS.map(k => secretOf(k)!);

/** `null` with no key saved; "refused" when one is saved and its models could not be read; else "saved". */
export function keyNote(secret: SecretState | undefined, info: ProviderInfo | undefined): 'saved' | 'refused' | null {
    if (!secret?.set) return null;
    return info?.available ? 'saved' : 'refused';
}

/** Something on this computer that answers without a key. */
export type FoundHere = {
    key: string;
    /** "ready": installed and not known to be signed out; "signed-out": its own command says so. */
    state: 'ready' | 'signed-out';
    hint: string | null;
    models: number;
};

/**
 * The model CLIs installed here, and Ollama when it answers. A CLI whose sign-in
 * cannot be told without a model call (`signed_in` null) counts as ready (CLIM1).
 */
export function foundHere(providers: Record<string, ProviderInfo>): FoundHere[] {
    const out: FoundHere[] = [];
    const ollama = providers.ollama;
    if (ollama?.available && ollama.models.length) {
        out.push({ key: 'ollama', state: 'ready', hint: null, models: ollama.models.length });
    }
    for (const key of CLI_PROVIDERS) {
        const info = providers[key];
        if (!info?.available) continue;
        const signedOut = info.cli?.signed_in === false;
        out.push({
            key,
            state: signedOut ? 'signed-out' : 'ready',
            hint: signedOut ? info.cli?.sign_in_hint ?? null : null,
            models: info.models.length,
        });
    }
    return out;
}

/** Whether there is nothing to start from: no key saved, nothing found here. */
export function nothingYet(providers: Record<string, ProviderInfo>, secrets: Record<string, SecretState>): boolean {
    return foundHere(providers).length === 0 && !KEY_NAMES.some(n => secrets[n]?.set);
}

/** The models the two pickers offer: every reachable provider's, leaving out a signed-out CLI's (Naveen, 2026-10-20). */
export function pickable(providers: Record<string, ProviderInfo>): ComboboxOption[] {
    const usable = Object.fromEntries(Object.entries(providers).filter(([, info]) => info.cli?.signed_in !== false));
    return modelOptions(usable);
}

/** Where the everyday picker starts: today's model when it can be picked, else the first that can. */
export function startingModel(current: string | undefined, options: ComboboxOption[]): string {
    if (current && options.some(o => o.value === current)) return current;
    return options[0]?.value ?? '';
}
