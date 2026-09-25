"use client";

/*
 * A stored secret, as a settings screen may show it (F201).
 *
 * `GET /api/settings` serves no key any more: each secret arrives as "" beside
 * `<name>_set` and `<name>_hint` (its last four characters). So a key field is
 * empty even when a key is saved, and saving the page with it empty keeps the
 * key. What a field needs to say is therefore "one is saved", and removing one
 * is a button of its own — emptying the field no longer does it.
 */
import { createContext, useContext } from "react";

export type SecretState = { set: boolean; hint: string };

export const SavedSecretsContext = createContext<{
    secrets: Record<string, SecretState>;
    remove: (name: string) => void;
}>({ secrets: {}, remove: () => {} });

export const useSavedSecrets = () => useContext(SavedSecretsContext);

/** `<name>_set` / `<name>_hint` from a settings payload, for every name given. */
export function pickSecrets(data: Record<string, unknown>, names: readonly string[]): Record<string, SecretState> {
    return Object.fromEntries(names.map(n => [n, {
        set: data[`${n}_set`] === true,
        hint: typeof data[`${n}_hint`] === "string" ? (data[`${n}_hint`] as string) : "",
    }]));
}

/** A key field's placeholder: say one is saved rather than invite a new one. */
export function secretPlaceholder(secrets: Record<string, SecretState>, name: string, fallback: string) {
    return secrets[name]?.set ? "Saved — type a new one to replace it" : fallback;
}

/** Under a key field: which key is saved, and a way to remove it. */
export function SavedSecret({ name, label, typed }: { name: string; label: string; typed: string }) {
    const { secrets, remove } = useSavedSecrets();
    const state = secrets[name];
    if (!state?.set || typed) return null;
    return (
        <p className="text-2xs text-text-faint flex items-center gap-2">
            <span>Saved{state.hint ? `, ending in ${state.hint}` : ""}.</span>
            <button
                type="button"
                onClick={() => remove(name)}
                aria-label={`Remove the saved ${label}`}
                className="text-danger hover:underline"
            >
                Remove
            </button>
        </p>
    );
}
