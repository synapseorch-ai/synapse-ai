/**
 * The AI tools that can use Synapse over MCP, and what to paste into each (GUIDE1, D318, F915).
 *
 * Naveen: "we should have a separate mcp section menu that says connect to claude, codex, cursor with mcp
 * configuration setting". Every snippet reaches the one door MCP2 built (D292): the product's MCP endpoint, with a
 * Synapse API key as its bearer, or `synapse mcp` (CLI1's stdio relay) for a client that only starts local
 * servers. Nothing here is a new way in. Synced into cloud's web with the rest of `components/connect`.
 */

export type AiClientId = 'claude_code' | 'claude_desktop' | 'codex' | 'cursor' | 'vscode';

/** How this product's Claude Desktop row connects: OSS relays over stdio; cloud signs in with OAuth when it's on. */
export type DesktopRoute = 'stdio' | 'oauth' | 'unavailable';

export type AiClient = {
    id: AiClientId;
    label: string;
    /** The host's logo for it (`logos`), or none for a letter tile. */
    logo?: 'claude' | 'openai';
    /** Where the snippet goes, in a few words. */
    where: string;
    /** The snippet's language, for its font and its copy label. */
    kind: 'shell' | 'json' | 'toml';
};

export const AI_CLIENTS: AiClient[] = [
    { id: 'claude_code', label: 'Claude Code', logo: 'claude', where: 'Run this in a terminal.', kind: 'shell' },
    {
        id: 'claude_desktop', label: 'Claude Desktop', logo: 'claude',
        where: 'In Claude Desktop: Settings → Developer → Edit Config, then restart Claude.', kind: 'json',
    },
    {
        id: 'codex', label: 'Codex', logo: 'openai',
        where: 'Run this in a terminal. Keep the export in your shell profile so Codex finds the key next time.',
        kind: 'shell',
    },
    { id: 'cursor', label: 'Cursor', where: 'In ~/.cursor/mcp.json, or .cursor/mcp.json inside a project.', kind: 'json' },
    { id: 'vscode', label: 'VS Code', where: 'In .vscode/mcp.json inside a project, then press Start above the entry.', kind: 'json' },
];

/** What a snippet is filled from. `key` empty draws the placeholder, so the shape is readable before a key exists. */
export type SnippetInput = { url: string; key: string; desktop: DesktopRoute };

export const KEY_PLACEHOLDER = 'sk-syn-…your key…';

const json = (value: unknown) => JSON.stringify(value, null, 2);

/** The text to paste into a client, or null when this product can't connect it (cloud's Desktop with OAuth off). */
export function snippetFor(client: AiClientId, { url, key, desktop }: SnippetInput): string | null {
    const bearer = `Bearer ${key || KEY_PLACEHOLDER}`;
    switch (client) {
        case 'claude_code':
            return `claude mcp add --transport http synapse ${url} \\\n  --header "Authorization: ${bearer}"`;
        case 'claude_desktop':
            // Desktop starts local servers from its config; a remote one is a connector it signs in to.
            if (desktop === 'stdio') return json({ mcpServers: { synapse: { command: 'synapse', args: ['mcp'] } } });
            return null;
        case 'codex':
            return `export SYNAPSE_API_KEY="${key || KEY_PLACEHOLDER}"\ncodex mcp add synapse --url ${url} --bearer-token-env-var SYNAPSE_API_KEY`;
        case 'cursor':
            return json({ mcpServers: { synapse: { url, headers: { Authorization: bearer } } } });
        case 'vscode':
            return json({ servers: { synapse: { type: 'http', url, headers: { Authorization: bearer } } } });
    }
}

/** Whether a client takes the key in its snippet (Desktop over stdio uses the CLI's own; over OAuth, a sign-in). */
export function needsKey(client: AiClientId, desktop: DesktopRoute): boolean {
    return client !== 'claude_desktop' || desktop === 'unavailable';
}

/** What you can ask once connected: each is a product tool the MCP server lists (MCP2's `core/product_tools`). */
export const WHAT_TO_ASK: { ask: string; does: string }[] = [
    { ask: 'Run my “Invoice follow-up” workflow and tell me what it returned.', does: 'Every workflow you offer to MCP is a tool of its own.' },
    { ask: 'What is waiting for my approval? Approve the refund one.', does: 'Answers a run’s questions and approvals, as you would here.' },
    { ask: 'Build an agent that sorts my support inbox by urgency.', does: 'Builds agents and workflows, when its key may build.' },
    { ask: 'What did my runs cost this week?', does: 'Reads runs, their results and what they spent.' },
];
