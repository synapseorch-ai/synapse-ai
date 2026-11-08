/*
 * A new agent handed to the agent editor, unsaved (SUB1a, S9: a helper's "Save
 * as agent"). The chat page puts it here and opens `/agents?draft=1`; the editor
 * takes it once and opens it as a new agent that nothing saves until the
 * person clicks Save. sessionStorage: one tab's, gone with it, never a server's.
 */
export type AgentDraft = { name: string; system_prompt: string; tools: string[]; model: string | null };

const KEY = 'synapse.agentDraft';

export function putAgentDraft(draft: AgentDraft): boolean {
    try {
        sessionStorage.setItem(KEY, JSON.stringify(draft));
        return true;
    } catch {
        return false;
    }
}

export function takeAgentDraft(): AgentDraft | null {
    try {
        const raw = sessionStorage.getItem(KEY);
        sessionStorage.removeItem(KEY);
        if (!raw) return null;
        const draft = JSON.parse(raw);
        if (!draft || typeof draft.name !== 'string' || typeof draft.system_prompt !== 'string') return null;
        return {
            name: draft.name,
            system_prompt: draft.system_prompt,
            tools: Array.isArray(draft.tools) ? draft.tools.filter((t: unknown) => typeof t === 'string') : [],
            model: typeof draft.model === 'string' && draft.model ? draft.model : null,
        };
    } catch {
        return null;
    }
}
