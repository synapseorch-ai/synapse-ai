/**
 * The AI tools that can use Synapse over MCP, and what to paste into each (GUIDE1, D318, F915).
 *
 * Naveen: "we should have a separate mcp section menu that says connect to claude, codex, cursor with mcp
 * configuration setting". Every snippet reaches the one door MCP2 built (D292): the product's MCP endpoint, with a
 * Synapse API key as its bearer, or `synapse mcp` (CLI1's stdio relay) for a client that only starts local
 * servers. Nothing here is a new way in. Synced into cloud's web with the rest of `components/connect`.
 */

import { DOCS_BASE } from '@/components/help';

export type AiClientId = 'claude_code' | 'claude_desktop' | 'codex' | 'cursor' | 'vscode';

/** The docs' own MCP server (DOC5): public, no key; what "Learn Synapse with your AI assistant" adds (F1030). */
export const DOCS_MCP_URL = `${DOCS_BASE}/api/mcp`;

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

/**
 * A one-click install, where the client has one (F1027; Naveen: "a button that will just get the things done of
 * connecting"): Cursor's deeplink takes the server's config as base64 JSON, VS Code's URL handler the same object
 * as `--add-mcp` takes, URL-encoded. The key rides in the link to the app on this machine, never over the network.
 * Claude Code and Codex have none (their button copies one command); Claude signs in instead (`CLAUDE_CONNECTORS`).
 */
export function installLink(
    client: AiClientId, { url, key, name = 'synapse' }: { url: string; key?: string; name?: string },
): string | null {
    // No key: a public server (the docs'), which takes no Authorization at all.
    const auth = key ? { headers: { Authorization: `Bearer ${key}` } } : {};
    if (client === 'cursor') {
        const config = btoa(JSON.stringify({ url, ...auth }));
        return `cursor://anysphere.cursor-deeplink/mcp/install?name=${encodeURIComponent(name)}&config=${encodeURIComponent(config)}`;
    }
    if (client === 'vscode') {
        return `vscode:mcp/install?${encodeURIComponent(JSON.stringify({ name, type: 'http', url, ...auth }))}`;
    }
    return null;
}

/** The one command that adds a public server (no key) to a terminal client; null for the others. */
export function publicCommand(client: AiClientId, url: string, name: string): string | null {
    if (client === 'claude_code') return `claude mcp add --transport http ${name} ${url}`;
    if (client === 'codex') return `codex mcp add ${name} --url ${url}`;
    return null;
}

/**
 * A key Home's card makes for one client: named for the client and where it came from, so the keys list says what
 * each is (F1027). The install link carries it into the app, so it is the least a client needs (cloud: run-only).
 */
export const homeKeyName = (client: string) => `${client}, from Home`;

/**
 * The keys a repeat click replaces: that client's Home keys never used yet. A used one is connected somewhere and is
 * kept; an unused one only ever lived in a link, so a new click revokes it rather than leaving a key per click.
 */
export function unusedHomeKeys(keys: { id: string; name: string; last_used_at: string | null }[], client: string): string[] {
    const name = homeKeyName(client);
    return keys.filter(k => k.name === name && !k.last_used_at).map(k => k.id);
}

/**
 * Where Claude (the app and claude.ai share them) adds a custom connector. Claude has no link that fills the form in,
 * so its button copies the address and opens this page: one paste, then Claude signs in with OAuth.
 */
export const CLAUDE_CONNECTORS = 'https://claude.ai/settings/connectors';

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
