"use client";

/*
 * Settings › MCP server (GUIDE1, D318, F915 → F1027): the shared screen, with this install's host. It was a rail item
 * of its own (Connect › AI tools); Naveen: "I don't want new ai tools menu ... let's have a button that takes to an
 * MCP server page in settings". Home's card (`ConnectCard`) connects most tools in one click from the same host.
 *
 * The MCP door is the backend's `/api/v1/mcp` behind the UI's own origin (`core/mcp_server/endpoint.py`), keys are
 * Settings › API keys' (`/api/settings/api-keys`), and Claude Desktop starts `synapse mcp`, CLI1's stdio relay.
 */
import { useCallback, useMemo, useSyncExternalStore } from 'react';

import { ConnectAiTools, type ConnectHost } from '@/components/connect/ConnectAiTools';
import { DOCS_BASE } from '@/components/help';

const noSubscribe = () => () => {};

/** This install's connect host, or null until the browser knows its own origin (the address people paste). */
export function useOssConnectHost(): ConnectHost | null {
    const origin = useSyncExternalStore(noSubscribe, () => window.location.origin, () => '');

    const lastUsed = useCallback(async (id: string) => {
        const res = await fetch('/api/settings/api-keys');
        if (!res.ok) throw new Error('Could not read the keys.');
        const keys: { id: string; last_used_at: string | null }[] = await res.json();
        return keys.find(k => k.id === id)?.last_used_at ?? null;
    }, []);

    return useMemo<ConnectHost | null>(() => (origin ? {
        mcpUrl: `${origin}/api/v1/mcp`,
        desktop: 'stdio',
        logos: { claude: '/claude-ai-icon.svg', openai: '/chatgpt-icon.svg', cursor: '/cursor-icon.svg', vscode: '/vscode-icon.svg' },
        // A key here can do all a person here can; there is no run-only kind to choose.
        makeKey: async (name: string) => {
            const res = await fetch('/api/settings/api-keys', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name }),
            });
            if (!res.ok) throw new Error('The key could not be made.');
            const data: { id: string; key: string } = await res.json();
            return { id: data.id, key: data.key };
        },
        lastUsed,
        listKeys: async () => {
            const res = await fetch('/api/settings/api-keys');
            if (!res.ok) throw new Error('Could not read the keys.');
            return res.json();
        },
        revokeKey: async (id: string) => {
            await fetch(`/api/settings/api-keys/${encodeURIComponent(id)}`, { method: 'DELETE' });
        },
        keysHref: '/settings/api_keys',
        guideHref: `${DOCS_BASE}/guides/developers/mcp`,
    } : null), [origin, lastUsed]);
}

export function McpServerTab() {
    const host = useOssConnectHost();
    return host ? <ConnectAiTools host={host} /> : null;
}
