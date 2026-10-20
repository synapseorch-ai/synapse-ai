/**
 * A provider key saved or removed on its own, through the settings route
 * (PROV1): what Settings → Models' cards and the first-run model step both call
 * (ONB2, D303). The route merges the one setting into what is stored and
 * answers with the settings as the browser may see them: no key, only
 * `<name>_set` and `<name>_hint` (F201).
 */

async function post(body: Record<string, unknown>, failure: string): Promise<Record<string, unknown>> {
    const res = await fetch('/api/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
    });
    if (!res.ok) {
        const said = await res.json().catch(() => ({}));
        throw new Error(typeof said?.detail === 'string' ? said.detail : failure);
    }
    return res.json();
}

/** Saves one key; throws with the refusal's words. */
export const postSecret = (name: string, value: string) => post({ [name]: value }, 'Could not save that key.');

/** Removes one key. */
export const clearSecret = (name: string) => post({ clear_secrets: [name] }, 'Could not remove the key.');
