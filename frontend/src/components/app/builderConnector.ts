/**
 * The self-hosted install's way to each Connect card (BWA1, D274): the routes
 * its own settings screens already use. What the person types goes from here
 * to this install's backend and nowhere else; the builder never sees it.
 *
 * There is no Apps catalog here, so an `app` card falls back to the Apps screen.
 */
import type { ConnectResult, Connector } from '@/components/assistant';

/** The screens a card can link to, by the id `prepare_connection` uses. */
const SCREENS: Record<string, string> = {
    models: '/settings/models',
    apps: '/apps',
    channels: '/channels',
    phone: '/channels/phone',
    widgets: '/widgets',
    tools: '/tools',
    variables: '/settings/variables',
    schedules: '/schedules',
};

/** A provider's key, as this install's settings name it. */
const PROVIDER_SETTING: Record<string, string> = {
    openai: 'openai_key', anthropic: 'anthropic_key', gemini: 'gemini_key', grok: 'grok_key',
    deepseek: 'deepseek_key', mistral: 'mistral_key', moonshot: 'moonshot_key', qwen: 'qwen_key',
};

async function problem(res: Response): Promise<string> {
    try {
        const body = await res.json();
        const detail = body?.detail ?? body?.message;
        if (typeof detail === 'string' && detail) return detail;
    } catch { /* not JSON */ }
    return `That did not work (${res.status}). Try again, or use the settings screen.`;
}

function post(path: string, body: unknown): Promise<Response> {
    return fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
}

/** A webhook platform's address on this install (`core/messaging/fields.py`'s `webhook_url`). */
const webhookUrl = (base: string, platform: string, id: string) =>
    `${base.replace(/\/+$/, '')}/api/messaging/${platform}/webhook/${encodeURIComponent(id)}`;

export const selfHostedConnector: Connector = {
    product: 'this install',
    href: (screen) => SCREENS[screen] ?? null,

    async server(server, token): Promise<ConnectResult> {
        const res = await post('/api/mcp/servers', {
            name: server.name, label: server.label ?? server.name, server_type: 'remote', url: server.url,
            token: token ?? '',
        });
        if (!res.ok) return { status: 'error', message: await problem(res) };
        const data = await res.json();
        if (data?.status === 'oauth_pending' && data?.auth_url) return { status: 'signin', url: data.auth_url };
        if (data?.connected) return { status: 'connected', message: `Connected ${server.label ?? server.name}.` };
        return { status: 'error', message: data?.message || 'Saved, but it did not connect yet. Check the Apps screen.' };
    },

    async providerKey(provider, key): Promise<ConnectResult> {
        const setting = PROVIDER_SETTING[provider.id];
        if (!setting) return { status: 'error', message: 'Add this provider on the Models screen.' };
        const res = await post('/api/settings', { [setting]: key });
        if (!res.ok) return { status: 'error', message: await problem(res) };
        return { status: 'connected', message: `Saved the ${provider.label ?? provider.id} key.` };
    },

    /** The Channels screen's own three calls (F1263): save it, give it its agent, start it. */
    async channel(channel, values): Promise<ConnectResult> {
        const credentials: Record<string, string> = Object.fromEntries(Object.entries(values).filter(([, v]) => v));
        if (channel.platform === 'whatsapp') credentials.whatsapp_mode = 'meta_api';
        const res = await post('/api/messaging/channels', {
            name: channel.name, platform: channel.platform, mode: channel.mode ?? 'polling', enabled: true, credentials,
        });
        if (!res.ok) return { status: 'error', message: await problem(res) };
        const saved = (await res.json()) as { id: string };
        if (channel.agent) {
            const assigned = await fetch(`/api/messaging/channels/${encodeURIComponent(saved.id)}/assignments`, {
                method: 'PUT', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ agent_ids: [channel.agent.id], default_agent_id: channel.agent.id }),
            });
            if (!assigned.ok) return { status: 'error', message: `Saved, but ${await problem(assigned)}` };
        }
        const started = await post(`/api/messaging/channels/${encodeURIComponent(saved.id)}/enable`, {});
        const now = started.ok ? ((await started.json()) as { status?: string; last_error?: string }) : null;
        if (!started.ok || now?.status === 'error') {
            const why = now?.last_error || (started.ok ? 'it did not start' : await problem(started));
            return { status: 'error', message: `Saved, but it did not start: ${why}. Check it on the Channels screen.` };
        }
        const who = channel.agent ? `, answered by ${channel.agent.name}` : '';
        if (channel.webhook_base) {
            return { status: 'connected', message: `Saved ${channel.label} "${channel.name}"${who}. It answers once ${channel.label} calls this app.`,
                webhookUrl: webhookUrl(channel.webhook_base, channel.platform, saved.id) };
        }
        return { status: 'connected', message: `Connected ${channel.label} "${channel.name}"${who}.` };
    },

    async toolSecret(tool, value, hostParams): Promise<ConnectResult> {
        if (tool.tools?.length) {
            // The tools a build made (F945): the server fills their host and checks it, and keeps the key.
            const res = await post('/api/tools/connect', {
                tools: tool.tools, header: tool.header, scheme: tool.scheme ?? '', key: value,
                host_params: hostParams ?? {}, provider: tool.name,
            });
            if (!res.ok) return { status: 'error', message: await problem(res) };
            const saved = (await res.json()) as { message?: string };
            return { status: 'connected', message: saved.message ?? `Saved the ${tool.header} for ${tool.name}.` };
        }
        const list = await fetch('/api/tools/custom');
        if (!list.ok) return { status: 'error', message: await problem(list) };
        const tools = (await list.json()) as Array<Record<string, unknown>>;
        const current = tools.find((t) => t.name === tool.name);
        if (!current) return { status: 'error', message: `No tool named ${tool.name} any more.` };
        const headers = { ...((current.headers as Record<string, string>) ?? {}), [tool.header]: value };
        const res = await post('/api/tools/custom', { ...current, headers });
        if (!res.ok) return { status: 'error', message: await problem(res) };
        return { status: 'connected', message: `Saved the ${tool.header} for ${tool.name}.` };
    },
};
