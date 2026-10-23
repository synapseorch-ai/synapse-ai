"use client";

/*
 * Connect › AI tools (GUIDE1, D318, F915): the shared screen with this install's host. The MCP door is the
 * backend's `/api/v1/mcp` behind the UI's own origin (`core/mcp_server/endpoint.py`), keys are Settings › API
 * keys' (`/api/settings/api-keys`), and Claude Desktop starts `synapse mcp`, CLI1's stdio relay.
 */
import { useCallback, useMemo, useSyncExternalStore } from 'react';

import { AppScreen } from '@/components/app/AppScreen';
import { ConnectAiTools, type ConnectHost } from '@/components/connect/ConnectAiTools';
import { DOCS_BASE } from '@/components/help';

const noSubscribe = () => () => {};

export function AiToolsScreen() {
    // The address people paste is this page's own origin; the server render has none, so it waits for the browser.
    const origin = useSyncExternalStore(noSubscribe, () => window.location.origin, () => '');

    const lastUsed = useCallback(async (id: string) => {
        const res = await fetch('/api/settings/api-keys');
        if (!res.ok) throw new Error('Could not read the keys.');
        const keys: { id: string; last_used_at: string | null }[] = await res.json();
        return keys.find(k => k.id === id)?.last_used_at ?? null;
    }, []);

    const host = useMemo<ConnectHost>(() => ({
        mcpUrl: `${origin}/api/v1/mcp`,
        desktop: 'stdio',
        logos: { claude: '/claude-ai-icon.svg', openai: '/chatgpt-icon.svg' },
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
        guideHref: `${DOCS_BASE}/api/mcp-server`,
    }), [origin, lastUsed]);

    return (
        <AppScreen id="ai_tools">
            {origin ? <ConnectAiTools host={host} /> : null}
        </AppScreen>
    );
}
