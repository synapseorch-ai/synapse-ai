'use client';

/*
 * OSS's side of the Phone tab (VOX1c): this server's routes mapped onto the
 * shapes in `components/phone/phone.ts`. Cloud keeps its own host; the screens
 * in `components/phone/` are the same in both.
 *
 * A number is a `messaging_channels` document with `platform: "twilio_voice"`:
 * it is read from the channel list (the Channels tab leaves it out), saved with
 * the channel save (a full replace, so the stored document is read first, and a
 * blank secret keeps the stored one), and turned on and off with the channel
 * routes. Its calls, the options and step two's numbers are VOX1a's and VOX1c's
 * voice routes; a call's transcript is its session's history.
 */
import { useEffect, useState } from 'react';

import { AppScreen } from '@/components/app/AppScreen';
import {
    PLATFORM, PhoneHostProvider, PhoneScreen, callFromRecord, numberFromChannel, pendingFrom, turnsFromHistory,
    type ConnectInput, type NumberPatch, type PhoneAgent, type PhoneHost, type PhoneNumber, type PhoneOptions,
    type VisitorToolsProps,
} from '@/components/phone';
import { VisitorToolGroups, type VisitorToolChoice } from '@/components/settings/VisitorToolGroups';

const enc = encodeURIComponent;

/** FastAPI's `detail`, a string or a list of field errors, as one sentence. */
async function refusal(res: Response, fallback: string): Promise<Error> {
    try {
        const body = await res.json();
        const detail = body?.detail;
        if (typeof detail === 'string' && detail) return new Error(detail);
        if (Array.isArray(detail) && detail.length) {
            return new Error(detail.map((d: { msg?: string }) => String(d?.msg ?? d).replace(/^Value error, /, '')).join(' '));
        }
    } catch { /* not JSON */ }
    return new Error(`${fallback} (HTTP ${res.status}).`);
}

async function call<T>(url: string, init: RequestInit | undefined, fallback: string): Promise<T> {
    const res = await fetch(url, init);
    if (!res.ok) throw await refusal(res, fallback);
    return res.json() as Promise<T>;
}

const json = (method: string, body: unknown): RequestInit => ({
    method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
});

type ChannelDoc = Record<string, unknown> & { id?: string; platform?: string; voice?: Record<string, unknown> };

async function channelDocs(): Promise<ChannelDoc[]> {
    const list = await call<unknown>('/api/messaging/channels', undefined, 'The numbers could not be read');
    return (Array.isArray(list) ? list : []).filter((c: ChannelDoc) => c?.platform === PLATFORM);
}

async function readNumber(id: string): Promise<PhoneNumber | null> {
    const doc = (await channelDocs()).find(c => c.id === id);
    return doc ? numberFromChannel(doc) : null;
}

async function saveDoc(id: string, change: (doc: ChannelDoc) => ChannelDoc): Promise<void> {
    const doc = (await channelDocs()).find(c => c.id === id);
    if (!doc) throw new Error('That number is gone. Refresh the list.');
    await call('/api/messaging/channels', json('POST', change(doc)), 'The number could not be saved');
}

async function assign(id: string, agentId: string): Promise<void> {
    // One agent answers a number (VOX1c): its default, and the only one whose tools a caller may be allowed.
    await call(`/api/messaging/channels/${enc(id)}/assignments`,
        json('PUT', { agent_ids: [agentId], default_agent_id: agentId }), 'The agent could not be set');
}

async function enable(id: string): Promise<PhoneNumber | null> {
    await call(`/api/messaging/channels/${enc(id)}/enable`, { method: 'POST' }, 'The number could not be turned on');
    return readNumber(id);
}

/** What visitors may use, from each answering agent's tools (as Channels and Widgets read them). */
function OssVisitorTools({ agentIds, ticked, onChange, disabled }: VisitorToolsProps) {
    const key = agentIds.join(',');
    const [tools, setTools] = useState<{ key: string; list: VisitorToolChoice[] } | null>(null);
    useEffect(() => {
        let gone = false;
        Promise.all(key.split(',').filter(Boolean).map(id =>
            fetch(`/api/widgets/visitor-tools?agent_id=${enc(id)}`).then(r => (r.ok ? r.json() : { tools: [] })).catch(() => ({ tools: [] }))))
            .then(lists => {
                const merged = new Map<string, VisitorToolChoice>();
                // A tool asks first if any of the number's agents asks first about it (F1524).
                for (const l of lists) for (const t of (l.tools ?? []) as VisitorToolChoice[])
                    merged.set(t.name, { ...t, asks_first: !!(merged.get(t.name)?.asks_first || t.asks_first) });
                if (!gone) setTools({ key, list: [...merged.values()].sort((a, b) => a.name.localeCompare(b.name)) });
            });
        return () => { gone = true; };
    }, [key]);
    const list = tools?.key === key ? tools.list : null;
    if (list === null) return <p className="m-0 text-[13px] text-text-faint">Reading the agent&apos;s tools…</p>;
    if (!list.length) return <p className="m-0 text-[13px] text-text-faint">This agent has no tools to offer.</p>;
    return (
        <fieldset disabled={disabled} className="m-0 min-w-0 border-0 p-0">
            <VisitorToolGroups tools={list} ticked={ticked} onChange={onChange} idPrefix="phone-caller-tool" subject="Callers" asked="held" />
        </fieldset>
    );
}

export const OSS_PHONE_HOST: PhoneHost = {
    numbers: async () => (await channelDocs()).map(numberFromChannel),
    options: () => call<PhoneOptions>(`/api/messaging/${PLATFORM}/options`, undefined, 'The options could not be read'),
    agents: async () => {
        const [agents, settings] = await Promise.all([
            call<unknown>('/api/agents', undefined, 'The agents could not be read'),
            fetch('/api/settings').then(r => (r.ok ? r.json() : {})).catch(() => ({})) as Promise<Record<string, unknown>>,
        ]);
        return (Array.isArray(agents) ? agents : []).map((a: Record<string, unknown>): PhoneAgent => ({
            id: String(a.id ?? ''),
            name: String(a.name || a.id || ''),
            model: String(a.model || settings.model || ''),
            thinking: String(a.reasoning_effort || settings.reasoning_effort || 'medium'),
            planning: a.planning !== false,
            workflow: a.type === 'orchestrator',
        })).filter(a => a.id);
    },
    calls: async numberId => {
        const body = await call<{ calls?: unknown[] }>(
            `/api/messaging/${PLATFORM}/channels/${enc(numberId)}/calls?limit=50`, undefined, 'The calls could not be read');
        return (body.calls ?? []).map(callFromRecord);
    },
    transcript: async c => {
        const body = await call<{ turns?: unknown; pending_approval?: unknown }>(
            `/api/sessions/${enc(c.sessionId)}/history?agent_id=${enc(c.agentId)}`, undefined, 'The conversation could not be read');
        return { turns: turnsFromHistory(body.turns), pending: pendingFrom(body.pending_approval) };
    },
    answer: async (c, approve, approvalId) => {
        await call(`/api/messaging/${PLATFORM}/approvals/${enc(c.sessionId)}`,
            json('POST', { approve, approval_id: approvalId }), 'That answer did not go through');
    },
    voicemail: async c => {
        const res = await fetch(`/api/messaging/${PLATFORM}/calls/${enc(c.id)}/voicemail`);
        if (!res.ok) throw await refusal(res, 'The voicemail could not be played');
        return res.blob();
    },
    modelCost: async c => {
        try {
            const body = await call<{ logs?: { estimated_cost?: number }[] }>(
                `/api/usage/logs?session_id=${enc(c.sessionId)}&limit=1000`, undefined, '');
            const logs = body.logs ?? [];
            return logs.length ? logs.reduce((sum, l) => sum + (Number(l.estimated_cost) || 0), 0) : null;
        } catch {
            return null;
        }
    },
    twilioNumbers: async key => {
        const body = await call<{ numbers?: [] }>(`/api/messaging/${PLATFORM}/numbers`,
            json('POST', { account_sid: key.accountSid, api_key_sid: key.keySid, api_key_secret: key.keySecret }),
            'Twilio did not accept the key');
        return body.numbers ?? [];
    },
    connect: async (input: ConnectInput) => {
        const saved = await call<ChannelDoc>('/api/messaging/channels', json('POST', {
            name: input.name,
            platform: PLATFORM,
            enabled: false,
            credentials: { account_sid: input.key.accountSid, api_key_sid: input.key.keySid, api_key_secret: input.key.keySecret },
            voice: { number: input.number, number_sid: input.numberSid, ...input.voice },
            senders_are_members: false,
            visitor_tools: [],
        }), 'The number could not be saved');
        const id = String(saved.id ?? '');
        await assign(id, input.agentId);
        // Turning it on checks the key and the number and points the number here; a refusal is the number's error.
        return (await enable(id)) ?? numberFromChannel(saved);
    },
    save: async (n: PhoneNumber, patch: NumberPatch) => {
        if (patch.agentId) await assign(n.id, patch.agentId);
        if (patch.name !== undefined || patch.visitorTools || patch.voice) {
            await saveDoc(n.id, doc => ({
                ...doc,
                ...(patch.name !== undefined ? { name: patch.name } : {}),
                ...(patch.visitorTools ? { visitor_tools: patch.visitorTools } : {}),
                ...(patch.voice ? { voice: { ...(doc.voice ?? {}), ...patch.voice } } : {}),
            }));
        }
        return (await readNumber(n.id)) ?? n;
    },
    setEnabled: async (n, on) => {
        if (on) return { number: await enable(n.id), left: [] };
        const body = await call<{ left?: string[] }>(`/api/messaging/channels/${enc(n.id)}/disable`, { method: 'POST' },
            'The number could not be turned off');
        return { number: await readNumber(n.id), left: body.left ?? [] };
    },
    remove: async n => {
        const body = await call<{ left?: string[] }>(`/api/messaging/channels/${enc(n.id)}`, { method: 'DELETE' },
            'The number could not be deleted');
        return { left: body.left ?? [] };
    },
    Frame: ({ actions, children }) => <AppScreen id="phone" width="wide" actions={actions}>{children}</AppScreen>,
    VisitorTools: OssVisitorTools,
    canEdit: true,
    canAnswer: true,
    credits: false,
    links: {
        agent: id => `/agents?id=${enc(id)}`,
        address: { label: 'Set it on the Channels tab.', href: '/channels' },
    },
    costLine: 'Twilio charges your Twilio account for each call: about $0.08 a minute in the US.',
    longestCall: '30 minutes',
};

/** The Phone tab with OSS's host: `?number=&call=` as the page read them. */
export function OssPhone({ number, call: callSid }: { number?: string; call?: string }) {
    return (
        <PhoneHostProvider value={OSS_PHONE_HOST}>
            <PhoneScreen number={number} call={callSid} />
        </PhoneHostProvider>
    );
}
