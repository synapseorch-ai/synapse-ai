/* eslint-disable @typescript-eslint/no-explicit-any */
"use client";
import React, { useState, useEffect, useCallback, useRef } from 'react';
import { AlertTriangle, FlaskConical, Info, Play, Plus, RefreshCw, Square, Trash } from 'lucide-react';
import { AppScreen } from '@/components/app/AppScreen';
import { VisitorToolGroups, type VisitorToolChoice } from '@/components/settings/VisitorToolGroups';
import { DetailHead, LIST_DETAIL, ListRow, showDetail } from '@/components/run';
import {
    Button, Checkbox, ConfirmDialog, EmptyState, ErrorNote, Field, IconButton, Input, Section, Segmented,
    Select, Toast, useToast, type ToastTone,
} from '@/components/ui';
import { cn } from '@/lib/cn';

// ─── Types ────────────────────────────────────────────────────────────────────

type Platform = 'telegram' | 'discord' | 'slack' | 'teams' | 'whatsapp';

interface Channel {
    id?: string;
    name: string;
    platform: Platform;
    /** Legacy single-agent scalar. Read for pre-join channels; never edited here
     *  any more — the assignment join is what this screen writes. */
    agent_id?: string;
    /** The join, default first. Sent back by every channel read. */
    agent_ids?: string[];
    multi_agent_mode: boolean;
    /** "polling" holds a connection to the platform; "webhook" holds nothing. */
    mode?: DeliveryMode;
    enabled: boolean;
    credentials: Record<string, string>;
    /** Which secret credentials are stored. The server never sends a secret
     *  back (F207): its field arrives blank, and a blank one on save keeps it. */
    credentials_set?: Record<string, boolean>;
    status?: 'running' | 'stopped' | 'error';
    last_error?: string | null;
    /** Whether the people messaging this bot are workspace users (F282). On
     *  unless set false — every channel saved before the setting existed. */
    senders_are_members?: boolean;
    /** When they are not: the tools a sender may use. None unless ticked. */
    visitor_tools?: string[];
}

type SenderToolChoice = VisitorToolChoice;

type DeliveryMode = 'polling' | 'webhook';

/** Platforms where delivery is a choice. Teams and WhatsApp are webhook-only
 *  by construction, and their adapters have always had their own fields. */
const MODE_CHOICE_PLATFORMS: Platform[] = ['telegram', 'discord', 'slack'];

const EMPTY_CHANNEL: Channel = {
    name: '',
    platform: 'telegram',
    agent_ids: [],
    multi_agent_mode: false,
    mode: 'polling',
    enabled: true,
    credentials: {},
};

// ─── Platform metadata ────────────────────────────────────────────────────────

const PLATFORMS: { id: Platform; label: string; icon: string }[] = [
    { id: 'telegram', label: 'Telegram', icon: '/telegram-icon.svg' },
    { id: 'discord', label: 'Discord', icon: '/discord-round-color-icon.svg' },
    { id: 'slack', label: 'Slack', icon: '/slack-icon.svg' },
    { id: 'teams', label: 'Teams', icon: '/teams.svg' },
    { id: 'whatsapp', label: 'WhatsApp', icon: '/whatsapp-color-icon.svg' },
];

// ─── Credential field definitions per platform ────────────────────────────────

/** `mode`: shown only for that delivery mode — webhook mode verifies each
 *  delivery with a credential polling never needs, and vice versa (F227). */
const CREDENTIAL_FIELDS: Record<Platform, { key: string; label: string; placeholder: string; secret?: boolean; note?: string; mode?: DeliveryMode }[]> = {
    telegram: [
        { key: 'bot_token', label: 'Bot token', placeholder: '123456:ABC-DEF...', secret: true, note: 'Get from @BotFather on Telegram' },
    ],
    discord: [
        { key: 'bot_token', label: 'Bot token', placeholder: 'MTE...', secret: true, note: 'From Discord Developer Portal → Bot → Token' },
        { key: 'channel_id', label: 'Restrict to channel ID (optional)', placeholder: '123456789012345678', note: 'Leave empty to respond in all channels where the bot is mentioned' },
        { key: 'application_id', label: 'Application ID', placeholder: '123456789012345678', mode: 'webhook', note: 'Developer Portal → General Information. Registers the /ask command' },
        { key: 'public_key', label: 'Public key', placeholder: 'a1b2c3…', mode: 'webhook', note: 'Developer Portal → General Information. Verifies each interaction Discord sends' },
    ],
    slack: [
        { key: 'bot_token', label: 'Bot token (xoxb-...)', placeholder: 'xoxb-...', secret: true, note: 'OAuth & Permissions → Bot Token Scopes' },
        { key: 'app_token', label: 'App-level token (xapp-...)', placeholder: 'xapp-...', secret: true, mode: 'polling', note: 'Enable Socket Mode → generate App-Level Token with connections:write scope' },
        { key: 'signing_secret', label: 'Signing secret', placeholder: '', secret: true, mode: 'webhook', note: 'Basic Information → App Credentials. Verifies each delivery Slack sends' },
    ],
    teams: [
        { key: 'app_id', label: 'Azure app ID', placeholder: 'xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx', note: 'Azure Bot resource → Configuration → Microsoft App ID' },
        { key: 'app_password', label: 'App password / client secret', placeholder: '', secret: true, note: 'Azure Bot → Certificates & Secrets' },
    ],
    whatsapp: [
        // These are conditionally rendered based on whatsapp_mode — see component
    ],
};

// ─── Setup guides per platform ────────────────────────────────────────────────

const SETUP_GUIDES: Record<Platform, { title: string; steps: string[] }> = {
    telegram: {
        title: 'Creating a Telegram Bot',
        steps: [
            'Open Telegram and search for @BotFather',
            'Send /newbot and follow the prompts to name your bot',
            'Copy the bot token shown (format: 123456:ABC-DEF...)',
            'Paste it in the Bot Token field above',
            'Click Save & Connect — the bot will start polling immediately',
        ],
    },
    discord: {
        title: 'Creating a Discord Bot',
        steps: [
            'Go to discord.com/developers/applications → New Application',
            'In the Bot tab, click Add Bot → copy the Token',
            'Enable Message Content Intent under Privileged Gateway Intents',
            'In OAuth2 → URL Generator: select bot + applications.commands, check Read/Send Messages',
            'Use the generated URL to invite the bot to your server',
            'Paste the bot token above and optionally restrict to a channel ID',
        ],
    },
    slack: {
        title: 'Creating a Slack App with Socket Mode',
        steps: [
            'Go to api.slack.com/apps → Create New App → From Scratch',
            'In Socket Mode, enable it and generate an App-Level Token (xapp-...) with connections:write scope',
            'In Event Subscriptions → Subscribe to bot events: message.im, app_mention',
            'In OAuth & Permissions add scopes: app_mentions:read, chat:write, im:history, im:write, channels:history',
            'Install to workspace and copy the Bot Token (xoxb-...)',
            'Paste both tokens above',
        ],
    },
    teams: {
        title: 'Creating a Microsoft Teams Bot',
        steps: [
            'Go to portal.azure.com → Create a resource → Azure Bot',
            'Set the Messaging endpoint to: https://<your-synapse-address>/api/messaging/teams/webhook/<channel-id>',
            'Use the public address of the Synapse app, not the backend port: the app is what passes a delivery on to the backend',
            'For local dev, tunnel the app (for example ngrok http 3000) and use the Forwarding URL',
            'In the bot Configuration, copy the Microsoft App ID',
            'In Certificates & Secrets, create a new client secret and copy it',
            'Add the Teams channel in the bot\'s Channels tab',
        ],
    },
    whatsapp: {
        title: 'WhatsApp Setup',
        steps: [
            'BUSINESS ACCOUNT PATH: Go to developers.facebook.com → My Apps → Create App (type: Business)',
            'Add WhatsApp product → Phone Numbers → select a test or production number',
            'In WhatsApp → Configuration, set Webhook URL to: https://<your-synapse-address>/api/messaging/whatsapp/webhook/<channel-id> (the public address of the Synapse app, not the backend port)',
            'Set your Verify Token (any string), subscribe to the messages webhook field',
            'Copy the Phone Number ID and generate a temporary/permanent Access Token',
            'In App settings → Basic, copy the App Secret: Synapse refuses any delivery it does not sign',
            '--- OR ---',
            'UNOFFICIAL PATH: No business account needed. Synapse will open WhatsApp Web in a browser window and you scan the QR code to authenticate. This may violate WhatsApp\'s Terms of Service.',
        ],
    },
};

/** A secret field's placeholder: say it is stored rather than show it. */
const credPlaceholder = (draft: Channel, key: string, secret: boolean | undefined, fallback: string) =>
    secret && draft.credentials_set?.[key] ? 'Saved — leave blank to keep' : fallback;

// ─── Status: a quiet dot and a word ───────────────────────────────────────────

const STATUS: Record<string, { word: string; dot: string }> = {
    running: { word: 'Running', dot: 'bg-success' },
    error: { word: 'Error', dot: 'bg-danger' },
    stopped: { word: 'Stopped', dot: 'bg-text-faint' },
};

const StatusWord = ({ status, lastError }: { status?: string; lastError?: string | null }) => {
    const s = STATUS[status ?? 'stopped'] ?? STATUS.stopped;
    return (
        <span className={cn('inline-flex items-center gap-1.5', status === 'error' && 'text-danger')} title={status === 'error' ? lastError || '' : undefined}>
            <span className={cn('size-1.5 rounded-full', s.dot)} aria-hidden />
            {s.word}
        </span>
    );
};

/** The old toasts led with an emoji for their tone; the kit's toast carries the tone itself. */
const TONE_OF: Record<string, ToastTone> = { '✅': 'success', '⚠️': 'warning', '❌': 'danger' };

// ─── Main component ───────────────────────────────────────────────────────────

export const MessagingTab = () => {
    const [channels, setChannels] = useState<Channel[]>([]);
    const [selectedId, setSelectedId] = useState<string | null>(null);
    const [draft, setDraft] = useState<Channel | null>(null);
    const [agents, setAgents] = useState<any[]>([]);
    const [saving, setSaving] = useState(false);
    const [testing, setTesting] = useState(false);
    const { toast, show, dismiss } = useToast();
    const detailRef = useRef<HTMLElement>(null);
    const [refreshing, setRefreshing] = useState(false);
    const [assigning, setAssigning] = useState(false);

    // WhatsApp sub-mode UI state
    const [waMode, setWaMode] = useState<'meta_api' | 'unofficial'>('meta_api');
    const [waRiskAck, setWaRiskAck] = useState(false);
    const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null);

    // Channel that mirrors run notifications ("" = in-app only)
    const [notifyChannelId, setNotifyChannelId] = useState('');
    // Public HTTPS address webhook-mode channels register with their platform.
    const [webhookBase, setWebhookBase] = useState('');
    const [webhookBaseDraft, setWebhookBaseDraft] = useState('');
    // The tools the channel's agents hold, for the sender setting (F282):
    // keyed by the assignment, so a changed set never shows the old list.
    const [toolChoices, setToolChoices] = useState<{ key: string; tools: SenderToolChoice[] } | null>(null);
    const assignedKey = (draft?.agent_ids?.length ? draft.agent_ids : draft?.agent_id ? [draft.agent_id] : []).join(',');
    useEffect(() => {
        if (!assignedKey) return;
        let cancelled = false;
        void (async () => {
            const lists = await Promise.all(assignedKey.split(',').map(id =>
                fetch(`/api/widgets/visitor-tools?agent_id=${encodeURIComponent(id)}`)
                    .then(r => (r.ok ? r.json() : { tools: [] }))
                    .catch(() => ({ tools: [] }))));
            const merged = new Map<string, SenderToolChoice>();
            for (const list of lists) for (const t of (list.tools ?? []) as SenderToolChoice[]) merged.set(t.name, t);
            if (!cancelled) {
                setToolChoices({ key: assignedKey, tools: [...merged.values()].sort((a, b) => a.name.localeCompare(b.name)) });
            }
        })();
        return () => { cancelled = true; };
    }, [assignedKey]);
    const agentNameRef = useRef('');  // Settings.agent_name is required on POST
    useEffect(() => {
        fetch('/api/settings').then(r => r.json())
            .then(d => {
                setNotifyChannelId(d.run_notification_channel_id || '');
                setWebhookBase(d.messaging_webhook_base_url || '');
                setWebhookBaseDraft(d.messaging_webhook_base_url || '');
                agentNameRef.current = d.agent_name || 'Synapse';
            })
            .catch(() => {});
    }, []);
    // POST /api/settings merges with exclude_unset, so sending two keys does not
    // clear the rest. agent_name rides along because the model requires it.
    const patchSettings = async (patch: Record<string, unknown>) =>
        fetch('/api/settings', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ agent_name: agentNameRef.current, ...patch }),
        });
    const saveNotifyChannel = async (channelId: string) => {
        setNotifyChannelId(channelId);
        try {
            await patchSettings({ run_notification_channel_id: channelId });
            showToast(channelId ? 'Run notifications enabled for channel' : 'Run notifications set to in-app only');
        } catch {
            showToast('Failed to save notification channel', 'danger');
        }
    };
    const saveWebhookBase = async () => {
        const value = webhookBaseDraft.trim().replace(/\/+$/, '');
        try {
            await patchSettings({ messaging_webhook_base_url: value });
            setWebhookBase(value);
            setWebhookBaseDraft(value);
            showToast(value ? 'Webhook address saved' : 'Webhook address cleared');
        } catch {
            showToast('Failed to save the webhook address', 'danger');
        }
    };

    const showToast = (msg: string, tone?: ToastTone) => {
        const lead = Object.keys(TONE_OF).find(e => msg.startsWith(e));
        show(lead ? msg.slice(lead.length).trim() : msg, tone ?? (lead ? TONE_OF[lead] : 'success'));
    };

    const fetchChannels = useCallback(async (): Promise<Channel[]> => {
        setRefreshing(true);
        try {
            const res = await fetch('/api/messaging/channels');
            if (res.ok) {
                const data = await res.json();
                // Phone numbers live on the Phone tab (V29, VOX1c): this screen knows no `twilio_voice`, and a
                // number opened here first threw (F447). Nor can one carry run notifications.
                const list = (Array.isArray(data) ? data : []).filter((c: { platform?: string }) => c.platform !== 'twilio_voice');
                setChannels(list);
                return list;
            }
        } catch {
            // ignore — messaging may not be enabled
        } finally {
            setRefreshing(false);
        }
        return [];
    }, []);

    const selectChannel = useCallback((ch: Channel) => {
        setSelectedId(ch.id || null);
        setDraft({ ...ch });
        if (ch.platform === 'whatsapp') {
            setWaMode((ch.credentials?.whatsapp_mode as any) || 'meta_api');
            setWaRiskAck(false);
        }
    }, []);

    useEffect(() => {
        // UX1: never an empty pane — the first channel opens.
        fetchChannels().then(list => { if (list[0]) selectChannel(list[0]); });
        fetch('/api/agents').then(r => r.json()).then(d => setAgents(Array.isArray(d) ? d : [])).catch(() => {});
    }, [fetchChannels, selectChannel]);

    const newChannel = () => {
        setSelectedId(null);
        setDraft({ ...EMPTY_CHANNEL });
        setWaMode('meta_api');
        setWaRiskAck(false);
        showDetail(detailRef.current);
    };

    const saveAndConnect = async () => {
        if (!draft) return;
        if (!draft.name.trim()) { showToast('Please enter a channel name', 'warning'); return; }
        if (draft.mode === 'webhook' && !webhookBase) {
            showToast('Set the public webhook address below before using webhook mode', 'warning');
            return;
        }

        setSaving(true);
        try {
            // Inject waMode into credentials for whatsapp
            const base = draft.platform === 'whatsapp'
                ? { ...draft, credentials: { ...draft.credentials, whatsapp_mode: waMode } }
                : draft;
            // `agent_ids` is a read projection of the join, and `agent_id` is the
            // legacy scalar the join replaced — POSTing either back would have
            // save_channel mirror an agent into the join behind the chips.
            const { agent_id: _legacy, agent_ids: _joined, ...payload } = base;
            void _legacy; void _joined;

            const res = await fetch('/api/messaging/channels', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
            });
            if (!res.ok) throw new Error('Save failed');
            const saved = await res.json();

            // Enable. It answers with the channel as it now stands: a token the
            // platform refused is `error` with the reason, not "started" (F207).
            const enableRes = await fetch(`/api/messaging/channels/${saved.id}/enable`, { method: 'POST' });
            const started: Channel | null = enableRes.ok ? await enableRes.json().catch(() => null) : null;

            await fetchChannels();
            setSelectedId(saved.id);
            // `saved` is the channel document; `agent_ids` is a read projection
            // of the join that only the list route adds. Saving changes no
            // assignment, so carry the ones already on screen.
            setDraft({ ...(started ?? saved), agent_ids: draft.agent_ids ?? [] });
            showToast(started?.status === 'error'
                ? `⚠️ Saved, but it did not start: ${started.last_error || 'unknown error'}`
                : '✅ Channel saved and started');
        } catch (e: any) {
            showToast(`❌ ${e.message}`);
        } finally {
            setSaving(false);
        }
    };

    const testChannel = async () => {
        if (!selectedId) return;
        setTesting(true);
        try {
            const res = await fetch(`/api/messaging/channels/${selectedId}/test`, { method: 'POST' });
            const data = await res.json();
            showToast(data.status === 'ok' ? `✅ ${data.message}` : `⚠️ ${data.message}`);
        } catch {
            showToast('❌ Test failed');
        } finally {
            setTesting(false);
        }
    };

    const toggleEnable = async (ch: Channel) => {
        if (!ch.id) return;
        const endpoint = ch.status === 'running' ? 'disable' : 'enable';
        await fetch(`/api/messaging/channels/${ch.id}/${endpoint}`, { method: 'POST' });
        await fetchChannels();
    };

    const deleteChannel = async (id: string) => {
        const ch = channels.find(c => c.id === id);
        if (!ch || !ch.id) return;
        await fetch(`/api/messaging/channels/${ch.id}`, { method: 'DELETE' });
        const list = await fetchChannels();
        // The open one went: the first that is left opens in its place.
        if (selectedId === ch.id) {
            if (list[0]) selectChannel(list[0]);
            else { setSelectedId(null); setDraft(null); }
        }
    };

    /** One whole-list write of the channel's agents. The chips describe a set,
     *  so the request describes a set — membership and the default move
     *  together instead of in three calls that can disagree in between. */
    const putAssignments = async (
        channelId: string,
        agentIds: string[],
        defaultAgentId?: string,
    ) => {
        setAssigning(true);
        try {
            const res = await fetch(`/api/messaging/channels/${channelId}/assignments`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ agent_ids: agentIds, default_agent_id: defaultAgentId }),
            });
            if (!res.ok) {
                const body = await res.json().catch(() => ({}));
                throw new Error(body.detail || 'Could not save the assignment');
            }
            const rows = await res.json();
            const ordered: string[] = [
                ...rows.filter((r: any) => r.is_default).map((r: any) => r.target_id),
                ...rows.filter((r: any) => !r.is_default).map((r: any) => r.target_id),
            ];
            setDraft(d => (d && d.id === channelId ? { ...d, agent_ids: ordered, agent_id: undefined } : d));
            setChannels(cs => cs.map(c => (c.id === channelId ? { ...c, agent_ids: ordered } : c)));
        } catch (e: any) {
            showToast(`❌ ${e.message}`);
        } finally {
            setAssigning(false);
        }
    };

    const toggleAgent = (agentId: string) => {
        if (!draft?.id) return;
        const current = draft.agent_ids ?? [];
        const next = current.includes(agentId)
            ? current.filter(a => a !== agentId)
            : [...current, agentId];
        putAssignments(draft.id, next);
    };

    const makeDefault = (agentId: string) => {
        if (!draft?.id) return;
        putAssignments(draft.id, draft.agent_ids ?? [], agentId);
    };

    const updateCred = (key: string, value: string) => {
        if (!draft) return;
        setDraft({ ...draft, credentials: { ...draft.credentials, [key]: value } });
    };

    const platform = draft?.platform || 'telegram';
    const platformMeta = PLATFORMS.find(p => p.id === platform)!;
    const capsLabel = 'text-sm font-medium text-text';
    const guide = SETUP_GUIDES[platform];
    /* The join, default first — with the pre-join scalar standing in for a
       channel that has never been assigned through it. */
    const assignedIds: string[] = draft?.agent_ids?.length
        ? draft.agent_ids
        : draft?.agent_id ? [draft.agent_id] : [];
    const agentNames = (ids: string[]) =>
        ids.map(id => agents.find(a => a.id === id)?.name || id);
    const webhookUrl = draft?.id && webhookBase
        ? `${webhookBase}/api/messaging/${platform}/webhook/${draft.id}`
        : '';
    const sendersAreMembers = draft?.senders_are_members !== false;
    const senderTools = toolChoices?.key === assignedIds.join(',') ? toolChoices.tools : null;
    const memoryAgents = agentNames(assignedIds.filter(id => agents.find(a => a.id === id)?.memory));

    const agentLine = (ch: Channel) => {
        const ids = ch.agent_ids?.length ? ch.agent_ids : ch.agent_id ? [ch.agent_id] : [];
        if (ids.length === 0) return 'no agents';
        if (ids.length <= 2) return agentNames(ids).join(', ');
        return `${agentNames(ids)[0]} +${ids.length - 1} more`;
    };
    const credInput = (f: { key: string; placeholder: string; secret?: boolean }) => (
        <Input
            id={`messaging-cred-${f.key}`}
            type={f.secret ? 'password' : 'text'}
            value={draft?.credentials[f.key] || ''}
            onChange={e => updateCred(f.key, e.target.value)}
            placeholder={draft ? credPlaceholder(draft, f.key, f.secret, f.placeholder) : f.placeholder}
            className="font-code"
        />
    );

    return (
        <AppScreen
            id="messaging"
            width="wide"
            actions={
                <>
                    <IconButton icon={RefreshCw} label="Refresh" onClick={fetchChannels} className={cn(refreshing && '[&_svg]:animate-spin')} />
                    <Button onClick={newChannel}><Plus className="size-4" aria-hidden /> New channel</Button>
                </>
            }
        >
            <div className={LIST_DETAIL}>
                {/* ── The channels, and what holds for all of them ─────────── */}
                <div className="grid min-w-0 content-start gap-6">
                    <nav aria-label="Channels">
                        {channels.length === 0 ? (
                            <p className="m-0 border-t border-border px-2.5 py-3 text-sm text-text-faint">
                                No channels configured yet. Use New channel to add one.
                            </p>
                        ) : (
                            <ul className="m-0 list-none border-t border-border p-0">
                                {channels.map(ch => {
                                    const pm = PLATFORMS.find(p => p.id === ch.platform);
                                    return (
                                        <ListRow
                                            key={ch.id}
                                            data={{ 'data-channel-row': ch.id ?? '' }}
                                            // The icon is decoration: the platform is said once, in words.
                                            leading={<img src={pm?.icon} alt="" aria-hidden className="size-4 shrink-0 object-contain" />}
                                            title={<>{ch.name}{pm?.label && <span className="sr-only">, {pm.label}</span>}</>}
                                            sub={agentLine(ch)}
                                            meta={<StatusWord status={ch.status} lastError={ch.last_error} />}
                                            current={selectedId === ch.id}
                                            onClick={() => { selectChannel(ch); showDetail(detailRef.current); }}
                                            actions={
                                                <>
                                                    <IconButton
                                                        size="sm"
                                                        icon={ch.status === 'running' ? Square : Play}
                                                        label={`${ch.status === 'running' ? 'Stop' : 'Start'} ${ch.name}`}
                                                        title={ch.status === 'running' ? 'Stop' : 'Start'}
                                                        onClick={() => toggleEnable(ch)}
                                                    />
                                                    <IconButton
                                                        size="sm"
                                                        icon={Trash}
                                                        tone="danger"
                                                        label={`Delete ${ch.name}`}
                                                        title="Delete"
                                                        onClick={() => setConfirmDeleteId(ch.id || null)}
                                                    />
                                                </>
                                            }
                                        />
                                    );
                                })}
                            </ul>
                        )}
                    </nav>

                    <Section title="For every channel" variant="plain" collapsible={false}>
                        <div className="grid gap-4">
                            <Field
                                label="Run notifications"
                                hint="Mirror orchestration notifications (needs input, completed, failed) to this channel's notify chat."
                            >
                                <Select
                                    value={notifyChannelId || '__off__'}
                                    onChange={v => saveNotifyChannel(v === '__off__' ? '' : v)}
                                    aria-label="Notification channel"
                                    size="sm"
                                    options={[
                                        { value: '__off__', label: 'Off (in-app only)' },
                                        // `Channel.id` is optional until the row is saved.
                                        ...channels.filter(ch => ch.id).map(ch => ({ value: ch.id as string, label: ch.name })),
                                    ]}
                                />
                            </Field>
                            <Field
                                label="Webhook address"
                                hint="Where platforms deliver messages for channels in webhook mode. A public HTTPS address of this Synapse app, not its backend port — a tunnel URL works for local testing."
                            >
                                <Input
                                    size="sm"
                                    type="text"
                                    value={webhookBaseDraft}
                                    onChange={e => setWebhookBaseDraft(e.target.value)}
                                    onBlur={saveWebhookBase}
                                    onKeyDown={e => { if (e.key === 'Enter') saveWebhookBase(); }}
                                    placeholder="https://synapse.example.com"
                                    aria-label="Public webhook base URL"
                                    className="font-code"
                                />
                            </Field>
                        </div>
                    </Section>
                </div>

                {/* ── The open channel ──────────────────────────────────── */}
                <section ref={detailRef} aria-label="Channel" className="min-w-0 scroll-mt-4">
                {draft ? (
                    <div className="grid gap-6">
                        <DetailHead
                            leading={<img src={platformMeta.icon} alt="" aria-hidden className="mt-0.5 size-7 shrink-0 object-contain" />}
                            title={selectedId ? (draft.name || 'Channel') : 'New channel'}
                            sub={selectedId
                                ? <span className="inline-flex items-center gap-2">{platformMeta.label} · <StatusWord status={draft.status} lastError={draft.last_error} /></span>
                                : platformMeta.label}
                            actions={
                                <>
                                    {selectedId && (
                                        <Button variant="secondary" onClick={testChannel} loading={testing}>
                                            {!testing && <FlaskConical className="size-4" aria-hidden />} Test
                                        </Button>
                                    )}
                                    <Button onClick={saveAndConnect} loading={saving}>Save &amp; connect</Button>
                                </>
                            }
                        />

                        {draft.last_error && (
                            <ErrorNote>
                                <span className="font-medium">Last error</span>
                                <span className="mt-0.5 block font-code text-xs">{draft.last_error}</span>
                            </ErrorNote>
                        )}

                        {/* Platform picker */}
                        <div className="grid gap-1.5">
                            <span className={capsLabel}>Platform</span>
                            <Segmented
                                label="Platform"
                                value={draft.platform}
                                onChange={p => setDraft({ ...EMPTY_CHANNEL, platform: p, name: draft.name })}
                                options={PLATFORMS.map(p => ({
                                    value: p.id,
                                    label: p.label,
                                    icon: <img src={p.icon} alt="" aria-hidden className="size-4 object-contain" />,
                                }))}
                            />
                        </div>

                        <div className="grid gap-1.5">
                            <label htmlFor="messaging-channel-name" className="block text-sm font-medium text-text">Channel name</label>
                            <Input
                                id="messaging-channel-name"
                                type="text"
                                value={draft.name}
                                onChange={e => setDraft({ ...draft, name: e.target.value })}
                                placeholder="e.g. Support Bot"
                            />
                        </div>

                        {/* ── Agents on this channel ─────────────────────── */}
                        <Section title="Agents" variant="plain" collapsible={false}>
                            <div className="grid gap-3">
                                <p className="m-0 text-[13px] text-text-muted">
                                    Who answers messages arriving here. A channel can front several
                                    agents; the one marked <strong className="font-semibold text-text">default</strong> takes
                                    a plain message.
                                </p>
                                {!draft.id ? (
                                    <p className="m-0 rounded-md bg-surface-2 px-3 py-2.5 text-[13px] text-text-muted">
                                        Save the channel first — agents are assigned to it once it exists.
                                    </p>
                                ) : agents.length === 0 ? (
                                    <p className="m-0 rounded-md bg-surface-2 px-3 py-2.5 text-[13px] text-text-muted">
                                        No agents configured yet. Create one on the Agents page.
                                    </p>
                                ) : (
                                    <>
                                        <div className="flex flex-wrap gap-2">
                                            {agents.map(a => {
                                                const on = assignedIds.includes(a.id);
                                                const isDefault = assignedIds[0] === a.id;
                                                return (
                                                    <span key={a.id} className="inline-flex items-center">
                                                        <button
                                                            type="button"
                                                            onClick={() => toggleAgent(a.id)}
                                                            disabled={assigning}
                                                            aria-pressed={on}
                                                            className={cn(
                                                                'inline-flex h-8 items-center rounded-full border px-3 text-[13px] font-medium transition-colors disabled:opacity-50',
                                                                on
                                                                    ? 'border-accent bg-accent text-accent-fg'
                                                                    : 'border-border text-text-muted hover:border-border-strong hover:text-text',
                                                                on && !isDefault && 'rounded-r-none',
                                                            )}
                                                        >
                                                            {a.name}
                                                            {isDefault && (
                                                                <span className="ml-2 text-2xs font-semibold uppercase opacity-80">default</span>
                                                            )}
                                                        </button>
                                                        {on && !isDefault && (
                                                            <button
                                                                type="button"
                                                                onClick={() => makeDefault(a.id)}
                                                                disabled={assigning}
                                                                title={`Make ${a.name} the default agent`}
                                                                className="inline-flex h-8 items-center rounded-r-full border border-l-0 border-accent px-2.5 text-2xs font-semibold uppercase text-text-muted transition-colors hover:text-text disabled:opacity-50"
                                                            >
                                                                set default
                                                            </button>
                                                        )}
                                                    </span>
                                                );
                                            })}
                                        </div>
                                        {assignedIds.length === 0 && (
                                            <p className="m-0 flex items-start gap-1.5 text-[13px] font-medium text-text">
                                                <AlertTriangle className="mt-0.5 size-3.5 shrink-0" aria-hidden />
                                                Nobody is assigned — this channel will reply saying so instead of answering.
                                            </p>
                                        )}
                                    </>
                                )}

                                <div className="flex items-start gap-3 pt-1">
                                    <Checkbox
                                        id="multi-agent-toggle"
                                        label="Multi-agent mode"
                                        checked={draft.multi_agent_mode}
                                        onChange={on => setDraft({ ...draft, multi_agent_mode: on })}
                                        className="mt-0.5"
                                    />
                                    <div className="grid flex-1 gap-1">
                                        <label htmlFor="multi-agent-toggle" className="cursor-pointer text-sm font-medium leading-tight text-text">
                                            Multi-agent mode
                                        </label>
                                        <p className="m-0 text-[13px] text-text-muted">
                                            When enabled, people in the chat can switch between the agents
                                            assigned above using <code className="rounded bg-surface-2 px-1 font-code">/agent &lt;name&gt;</code>,
                                            and list them with <code className="rounded bg-surface-2 px-1 font-code">/agents</code>.
                                            Only assigned agents are reachable; the default answers until somebody switches.
                                        </p>
                                        {draft.multi_agent_mode && assignedIds.length < 2 && (
                                            <p className="m-0 flex items-start gap-1.5 text-[13px] font-medium text-text">
                                                <AlertTriangle className="mt-0.5 size-3.5 shrink-0" aria-hidden />
                                                {assignedIds.length === 1
                                                    ? 'Only one agent is assigned, so there is nothing to switch between yet.'
                                                    : 'No agents are assigned, so there is nothing to switch between yet.'}
                                            </p>
                                        )}
                                    </div>
                                </div>
                            </div>
                        </Section>

                        {/* ── Who is messaging (F282) ────────────────────── */}
                        <Section title="Who messages this bot" variant="plain" collapsible={false}>
                            <div className="grid gap-3">
                                <div className="flex items-start gap-3">
                                    <Checkbox
                                        id="senders-are-members"
                                        label="Everyone who messages this bot is a workspace user"
                                        checked={sendersAreMembers}
                                        onChange={on => setDraft({ ...draft, senders_are_members: on })}
                                        className="mt-0.5"
                                    />
                                    <div className="grid flex-1 gap-1">
                                        <label htmlFor="senders-are-members" className="cursor-pointer text-sm font-medium leading-tight text-text">
                                            Everyone who messages this bot is a workspace user
                                        </label>
                                        <p className="m-0 text-[13px] text-text-muted">
                                            {sendersAreMembers
                                                ? 'They use the agents as you do: every tool, the agents\' memory, and a reply here can answer a workflow waiting on this channel. Keep it on for a bot only you or your team can reach.'
                                                : 'They are visitors: the agents use only the tools ticked below, never their memory, and a reply here does not answer a waiting workflow.'}
                                        </p>
                                        {sendersAreMembers && ((senderTools?.length ?? 0) > 0 || memoryAgents.length > 0) && (
                                            <p className="m-0 flex items-start gap-1.5 text-[13px] font-medium text-text">
                                                <AlertTriangle className="mt-0.5 size-3.5 shrink-0" aria-hidden />
                                                <span>
                                                    Anyone who can message this bot can use
                                                    {(senderTools?.length ?? 0) > 0 && (
                                                        <> {senderTools!.slice(0, 4).map(t => t.name).join(', ')}
                                                            {senderTools!.length > 4 && ` and ${senderTools!.length - 4} more`}</>
                                                    )}
                                                    {(senderTools?.length ?? 0) > 0 && memoryAgents.length > 0 && ', and'}
                                                    {memoryAgents.length > 0 && ` ${memoryAgents.join(', ')}'s memory`}.
                                                </span>
                                            </p>
                                        )}
                                    </div>
                                </div>
                                {!sendersAreMembers && (
                                    <div className="grid gap-2 pl-7">
                                        <span className={capsLabel}>What senders can use</span>
                                        {!assignedIds.length ? (
                                            <p className="m-0 text-[13px] text-text-faint">Assign an agent to choose its tools.</p>
                                        ) : senderTools === null ? (
                                            <p className="m-0 text-[13px] text-text-faint">Loading the agents&apos; tools…</p>
                                        ) : senderTools.length === 0 ? (
                                            <p className="m-0 text-[13px] text-text-faint">These agents have no tools to offer.</p>
                                        ) : (
                                            <VisitorToolGroups
                                                tools={senderTools}
                                                ticked={draft.visitor_tools ?? []}
                                                onChange={visitor_tools => setDraft({ ...draft, visitor_tools })}
                                                idPrefix="channel-sender-tool"
                                                subject="Senders"
                                            />
                                        )}
                                    </div>
                                )}
                            </div>
                        </Section>

                        {/* ── Delivery mode ──────────────────────────────── */}
                        {MODE_CHOICE_PLATFORMS.includes(draft.platform) && (
                            <Section title="Delivery" variant="plain" collapsible={false}>
                                <div className="grid gap-3">
                                    <p className="m-0 text-[13px] text-text-muted">
                                        How messages reach Synapse. Polling holds a connection open to the
                                        platform and needs no public address. Webhook holds nothing — the
                                        platform posts to you — which is what a multi-replica deployment
                                        wants, since only one process may poll a bot at a time.
                                    </p>
                                    {(() => {
                                        const modes: { value: DeliveryMode; label: string; hint: string }[] = [
                                            { value: 'polling', label: 'Polling', hint: 'Works anywhere. One process per bot.' },
                                            { value: 'webhook', label: 'Webhook', hint: 'Needs a public HTTPS address. Survives replicas.' },
                                        ];
                                        const current = draft.mode || 'polling';
                                        return (
                                            <div className="grid gap-1.5">
                                                <Segmented
                                                    label="Delivery"
                                                    value={current}
                                                    onChange={v => setDraft({ ...draft, mode: v })}
                                                    options={modes}
                                                />
                                                <p className="m-0 text-xs text-text-faint">{modes.find(m => m.value === current)?.hint}</p>
                                            </div>
                                        );
                                    })()}

                                    {draft.mode === 'webhook' && (
                                        <div className="grid gap-2 rounded-md bg-surface-2 p-3">
                                            {!webhookBase ? (
                                                <p className="m-0 flex items-start gap-1.5 text-[13px] font-medium text-text">
                                                    <AlertTriangle className="mt-0.5 size-3.5 shrink-0" aria-hidden />
                                                    Set the public webhook address below before saving — without it
                                                    the channel cannot register with {platformMeta?.label}.
                                                </p>
                                            ) : !draft.id ? (
                                                <p className="m-0 text-[13px] text-text-muted">
                                                    The delivery URL appears here once the channel is saved.
                                                </p>
                                            ) : (
                                                <div className="grid gap-1.5">
                                                    <label htmlFor="messaging-delivery-url" className="block text-sm font-medium text-text">Delivery URL</label>
                                                    <Input
                                                        id="messaging-delivery-url"
                                                        readOnly
                                                        value={webhookUrl}
                                                        onFocus={e => e.currentTarget.select()}
                                                        className="font-code text-xs"
                                                    />
                                                    <p className="m-0 text-xs text-text-faint">
                                                        {draft.platform === 'telegram'
                                                            ? 'Registered with Telegram automatically on Save & Connect.'
                                                            : `Paste this into your ${platformMeta?.label} app configuration.`}
                                                    </p>
                                                </div>
                                            )}
                                            {draft.platform === 'slack' && (
                                                <p className="m-0 text-xs text-text-faint">
                                                    Webhook mode does not need the app-level token (xapp-…) — that is
                                                    Socket Mode&apos;s, and Socket Mode is the polling option.
                                                </p>
                                            )}
                                        </div>
                                    )}
                                </div>
                            </Section>
                        )}

                        {/* Platform-specific credentials */}
                        <Section title={`Credentials — ${platformMeta?.label}`} variant="plain" collapsible={false}>
                            {draft.platform === 'whatsapp' ? (
                                <div className="grid gap-4">
                                    {(() => {
                                        const modes = [
                                            { value: 'meta_api' as const, label: 'Business (Meta API)', hint: 'Official. Requires Meta Business account + verified number.' },
                                            { value: 'unofficial' as const, label: 'Unofficial (Playwright)', hint: 'No business account. Uses WhatsApp Web automation.' },
                                        ];
                                        return (
                                            <div className="grid gap-1.5">
                                                <Segmented
                                                    label="WhatsApp mode"
                                                    value={waMode}
                                                    onChange={v => { setWaMode(v); setWaRiskAck(false); }}
                                                    options={modes}
                                                />
                                                <p className="m-0 text-xs text-text-faint">{modes.find(m => m.value === waMode)?.hint}</p>
                                            </div>
                                        );
                                    })()}

                                    {waMode === 'meta_api' && (
                                        <div className="grid gap-4">
                                            {[
                                                { key: 'phone_number_id', label: 'Phone number ID', placeholder: '123456789012345', secret: false },
                                                { key: 'access_token', label: 'Access token', placeholder: 'EAA...', secret: true },
                                                { key: 'app_secret', label: 'App secret', placeholder: '', secret: true,
                                                  note: 'App settings → Basic. Meta signs every delivery with it; a delivery without a matching signature is refused.' },
                                                { key: 'verify_token', label: 'Verify token (your choice)', placeholder: 'synapse_verify', secret: false,
                                                  note: 'Used to verify Meta webhook. Set the same value in your Meta App settings.' },
                                            ].map(f => (
                                                <div key={f.key} className="grid gap-1.5">
                                                    <label htmlFor={`messaging-cred-${f.key}`} className="block text-sm font-medium text-text">{f.label}</label>
                                                    {credInput(f)}
                                                    {f.note && <p className="m-0 text-xs text-text-faint">{f.note}</p>}
                                                </div>
                                            ))}
                                        </div>
                                    )}

                                    {waMode === 'unofficial' && (
                                        <div className="grid gap-3">
                                            {/* A person has to decide: the warm ground (UX1, amber is a person). */}
                                            <div className="flex gap-2.5 rounded-md border border-person/50 bg-person-subtle p-3 text-[13px] text-text">
                                                <AlertTriangle className="mt-0.5 size-4 shrink-0 text-person-ink" aria-hidden />
                                                <div className="grid gap-1">
                                                    <p className="m-0 font-semibold">Unofficial approach — use at your own risk</p>
                                                    <p className="m-0">This mode uses Playwright to drive WhatsApp Web in a browser window. It requires scanning a QR code. It may:</p>
                                                    <ul className="m-0 list-disc space-y-0.5 pl-4">
                                                        <li>Violate WhatsApp&apos;s Terms of Service</li>
                                                        <li>Break when WhatsApp updates their web app</li>
                                                        <li>Result in your number being temporarily banned</li>
                                                    </ul>
                                                    <p className="m-0">We recommend using the official Meta Business path instead.</p>
                                                </div>
                                            </div>
                                            <div className="flex items-start gap-3">
                                                <Checkbox
                                                    id="wa-risk-ack"
                                                    label="I understand the risks and accept responsibility for using the unofficial path."
                                                    checked={waRiskAck}
                                                    onChange={setWaRiskAck}
                                                    className="mt-0.5"
                                                />
                                                <label htmlFor="wa-risk-ack" className="cursor-pointer text-sm leading-tight text-text">
                                                    I understand the risks and accept responsibility for using the unofficial path.
                                                </label>
                                            </div>
                                            {waRiskAck && (
                                                <div className="grid gap-1 rounded-md bg-surface-2 p-3 text-[13px] text-text-muted">
                                                    <p className="m-0">After saving & connecting, Synapse will open WhatsApp Web in a Chromium browser window.</p>
                                                    <p className="m-0">Scan the QR code shown in the browser to authenticate. The session is saved and persists across restarts.</p>
                                                </div>
                                            )}
                                        </div>
                                    )}
                                </div>
                            ) : (
                                /* Standard credential fields */
                                <div className="grid gap-4">
                                    {CREDENTIAL_FIELDS[draft.platform]?.filter(f => !f.mode || f.mode === (draft.mode || 'polling')).map(f => (
                                        <div key={f.key} className="grid gap-1.5">
                                            <label htmlFor={`messaging-cred-${f.key}`} className="block text-sm font-medium text-text">{f.label}</label>
                                            {credInput(f)}
                                            {f.note && (
                                                <p className="m-0 flex items-center gap-1 text-xs text-text-faint">
                                                    <Info className="size-3 shrink-0" aria-hidden /> {f.note}
                                                </p>
                                            )}
                                        </div>
                                    ))}
                                </div>
                            )}
                        </Section>

                        {/* Setup guide, folded; it folds again for another channel or platform */}
                        <Section key={`${selectedId}:${platform}`} title={`Setup guide: ${guide.title}`} variant="plain">
                            <ol className="m-0 grid list-none gap-2 p-0">
                                {guide.steps.map((s, i) => (
                                    <li key={i} className="flex gap-2 text-[13px] text-text-muted">
                                        <span className="shrink-0 font-semibold text-text-faint">{s.startsWith('---') ? '' : `${i + 1}.`}</span>
                                        <span className={s.startsWith('---') ? 'w-full border-t border-border pt-1 text-text-faint' : ''}>{s}</span>
                                    </li>
                                ))}
                            </ol>
                        </Section>
                    </div>
                ) : (
                    <EmptyState
                        title="No channels yet"
                        action={<Button onClick={newChannel}><Plus className="size-4" aria-hidden /> New channel</Button>}
                    >
                        Reach your agents from Telegram, Discord, Slack, Teams or WhatsApp. Select a channel or create a new one.
                    </EmptyState>
                )}
                </section>
            </div>

            <ConfirmDialog
                open={!!confirmDeleteId}
                title="Delete channel?"
                description="Are you sure you want to delete this messaging channel? This will permanently remove its configuration and stop it if it is running."
                onConfirm={() => {
                    if (confirmDeleteId) deleteChannel(confirmDeleteId);
                    setConfirmDeleteId(null);
                }}
                onClose={() => setConfirmDeleteId(null)}
            />
            <Toast message={toast.message} tone={toast.tone} onDismiss={dismiss} />
        </AppScreen>
    );
};
