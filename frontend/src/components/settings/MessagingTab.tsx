/* eslint-disable @typescript-eslint/no-explicit-any */
import React, { useState, useEffect, useCallback, useRef } from 'react';
import {
    MessageSquare, Plus, Trash, Save, Play, Square, FlaskConical,
    ChevronDown, ChevronRight, AlertTriangle, CheckCircle, XCircle,
    Info, RefreshCw,
} from 'lucide-react';
import { ConfirmationModal } from './ConfirmationModal';
import { Select, Spinner } from '@/components/ui';

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
}

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

const PLATFORMS: { id: Platform; label: string; color: string; icon: string }[] = [
    { id: 'telegram', label: 'Telegram', color: 'text-sky-400 border-sky-800 bg-sky-950/40', icon: '/telegram-icon.svg' },
    { id: 'discord', label: 'Discord', color: 'text-indigo-400 border-indigo-800 bg-indigo-950/40', icon: '/discord-round-color-icon.svg' },
    { id: 'slack', label: 'Slack', color: 'text-green-400 border-green-800 bg-green-950/40', icon: '/slack-icon.svg' },
    { id: 'teams', label: 'Teams', color: 'text-blue-400 border-blue-800 bg-blue-950/40', icon: '/teams.svg' },
    { id: 'whatsapp', label: 'WhatsApp', color: 'text-emerald-400 border-emerald-800 bg-emerald-950/40', icon: '/whatsapp-color-icon.svg' },
];

// ─── Credential field definitions per platform ────────────────────────────────

/** `mode`: shown only for that delivery mode — webhook mode verifies each
 *  delivery with a credential polling never needs, and vice versa (F227). */
const CREDENTIAL_FIELDS: Record<Platform, { key: string; label: string; placeholder: string; secret?: boolean; note?: string; mode?: DeliveryMode }[]> = {
    telegram: [
        { key: 'bot_token', label: 'Bot Token', placeholder: '123456:ABC-DEF...', secret: true, note: 'Get from @BotFather on Telegram' },
    ],
    discord: [
        { key: 'bot_token', label: 'Bot Token', placeholder: 'MTE...', secret: true, note: 'From Discord Developer Portal → Bot → Token' },
        { key: 'channel_id', label: 'Restrict to Channel ID (optional)', placeholder: '123456789012345678', note: 'Leave empty to respond in all channels where the bot is mentioned' },
        { key: 'application_id', label: 'Application ID', placeholder: '123456789012345678', mode: 'webhook', note: 'Developer Portal → General Information. Registers the /ask command' },
        { key: 'public_key', label: 'Public Key', placeholder: 'a1b2c3…', mode: 'webhook', note: 'Developer Portal → General Information. Verifies each interaction Discord sends' },
    ],
    slack: [
        { key: 'bot_token', label: 'Bot Token (xoxb-...)', placeholder: 'xoxb-...', secret: true, note: 'OAuth & Permissions → Bot Token Scopes' },
        { key: 'app_token', label: 'App-Level Token (xapp-...)', placeholder: 'xapp-...', secret: true, mode: 'polling', note: 'Enable Socket Mode → generate App-Level Token with connections:write scope' },
        { key: 'signing_secret', label: 'Signing Secret', placeholder: '', secret: true, mode: 'webhook', note: 'Basic Information → App Credentials. Verifies each delivery Slack sends' },
    ],
    teams: [
        { key: 'app_id', label: 'Azure App ID', placeholder: 'xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx', note: 'Azure Bot resource → Configuration → Microsoft App ID' },
        { key: 'app_password', label: 'App Password / Client Secret', placeholder: '', secret: true, note: 'Azure Bot → Certificates & Secrets' },
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

// ─── Status badge ─────────────────────────────────────────────────────────────

const StatusBadge = ({ status, lastError }: { status?: string; lastError?: string | null }) => {
    if (status === 'running') return (
        <span className="flex items-center gap-1 text-[10px] font-bold text-green-400 bg-green-950/50 border border-green-900 px-2 py-0.5 rounded-md">
            <CheckCircle className="h-2.5 w-2.5" /> RUNNING
        </span>
    );
    if (status === 'error') return (
        <span className="flex items-center gap-1 text-[10px] font-bold text-red-400 bg-red-950/50 border border-red-900 px-2 py-0.5 rounded-md" title={lastError || ''}>
            <XCircle className="h-2.5 w-2.5" /> ERROR
        </span>
    );
    return (
        <span className="flex items-center gap-1 text-[10px] font-bold text-text-faint bg-zinc-900 border border-zinc-800 px-2 py-0.5 rounded-md">
            <Square className="h-2.5 w-2.5" /> STOPPED
        </span>
    );
};

// ─── Main component ───────────────────────────────────────────────────────────

export const MessagingTab = () => {
    const [channels, setChannels] = useState<Channel[]>([]);
    const [selectedId, setSelectedId] = useState<string | null>(null);
    const [draft, setDraft] = useState<Channel | null>(null);
    const [agents, setAgents] = useState<any[]>([]);
    const [saving, setSaving] = useState(false);
    const [testing, setTesting] = useState(false);
    const [toastMsg, setToastMsg] = useState<string | null>(null);
    const [guideOpen, setGuideOpen] = useState(false);
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
            showToast('Failed to save notification channel');
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
            showToast('Failed to save the webhook address');
        }
    };

    const showToast = (msg: string) => {
        setToastMsg(msg);
        setTimeout(() => setToastMsg(null), 3000);
    };

    const fetchChannels = useCallback(async () => {
        setRefreshing(true);
        try {
            const res = await fetch('/api/messaging/channels');
            if (res.ok) {
                const data = await res.json();
                setChannels(Array.isArray(data) ? data : []);
            }
        } catch {
            // ignore — messaging may not be enabled
        } finally {
            setRefreshing(false);
        }
    }, []);

    useEffect(() => {
        fetchChannels();
        fetch('/api/agents').then(r => r.json()).then(d => setAgents(Array.isArray(d) ? d : [])).catch(() => {});
    }, [fetchChannels]);

    const selectChannel = (ch: Channel) => {
        setSelectedId(ch.id || null);
        setDraft({ ...ch });
        setGuideOpen(false);
        if (ch.platform === 'whatsapp') {
            setWaMode((ch.credentials?.whatsapp_mode as any) || 'meta_api');
            setWaRiskAck(false);
        }
    };

    const newChannel = () => {
        setSelectedId(null);
        setDraft({ ...EMPTY_CHANNEL });
        setGuideOpen(false);
        setWaMode('meta_api');
        setWaRiskAck(false);
    };

    const saveAndConnect = async () => {
        if (!draft) return;
        if (!draft.name.trim()) { showToast('Please enter a channel name'); return; }
        if (draft.mode === 'webhook' && !webhookBase) {
            showToast('Set the public webhook address below before using webhook mode');
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
        if (selectedId === ch.id) { setSelectedId(null); setDraft(null); }
        await fetchChannels();
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

    return (
        <div className="grid grid-cols-1 md:grid-cols-12 gap-10">
            {/* Toast */}
            {toastMsg && (
                <div className="fixed bottom-6 right-6 z-50 bg-zinc-900 border border-zinc-700 text-white text-xs px-4 py-3 shadow-xl animate-in slide-in-from-bottom-4 rounded-md">
                    {toastMsg}
                </div>
            )}

            {/* ── Left: Channel List ─────────────────────────────────── */}
            <div className="md:col-span-4 border-r border-zinc-800 pr-4 flex flex-col max-h-[calc(100vh-180px)] sticky top-0 self-start">
                <div className="mb-4 flex justify-between items-center">
                    <h3 className="text-sm font-bold text-zinc-400">CHANNELS</h3>
                    <div className="flex gap-1">
                        <button
                            onClick={fetchChannels}
                            title="Refresh"
                            className="p-1.5 text-text-faint hover:text-white hover:bg-zinc-800 transition-colors"
                        >
                            <RefreshCw className={`h-3 w-3 ${refreshing ? 'animate-spin' : ''}`} />
                        </button>
                        <button
                            onClick={newChannel}
                            className="p-1.5 hover:bg-zinc-800 text-white transition-colors border border-dashed border-zinc-600 hover:border-white rounded-md"
                            title="New Channel"
                        >
                            <Plus className="h-4 w-4" />
                        </button>
                    </div>
                </div>

                <div className="space-y-2 flex-1 overflow-y-auto modern-scrollbar">
                    {channels.length === 0 && (
                        <div className="p-6 text-center text-text-faint text-xs border border-dashed border-zinc-800">
                            <MessageSquare className="h-8 w-8 mx-auto mb-2 opacity-20" />
                            No channels configured yet.<br />Click + to add one.
                        </div>
                    )}
                    {channels.map(ch => {
                        const pm = PLATFORMS.find(p => p.id === ch.platform);
                        const isSelected = selectedId === ch.id;
                        return (
                            // The row is a button, so a keyboard opens a channel as a click
                            // does; its quick actions are siblings, shown on focus as well as
                            // hover, and named (F236).
                            <div
                                key={ch.id}
                                className={`border transition-all group relative
                                    ${isSelected ? 'bg-zinc-900 border-white' : 'bg-black border-zinc-800 hover:border-zinc-600'} rounded-md`}
                            >
                                <button
                                    type="button"
                                    onClick={() => selectChannel(ch)}
                                    aria-current={isSelected ? 'true' : undefined}
                                    className="flex w-full items-center gap-3 p-3 pr-24 text-left"
                                >
                                    {/* The icon is decoration: inside a button its alt was read out
                                        beside the name, so the platform is said once, in words. */}
                                    <span aria-hidden className={`h-8 w-8 flex-shrink-0 flex items-center justify-center border ${pm?.color ?? ''}`}>
                                        <img src={pm?.icon} alt="" className="h-5 w-5 object-contain" />
                                    </span>
                                    <span className="block flex-1 min-w-0">
                                        <span className="block text-xs font-bold text-white truncate">
                                            {ch.name}
                                            {pm?.label && <span className="sr-only">, {pm.label}</span>}
                                        </span>
                                        <span className="block text-[10px] text-text-faint truncate">
                                            {(() => {
                                                const ids = ch.agent_ids?.length
                                                    ? ch.agent_ids
                                                    : ch.agent_id ? [ch.agent_id] : [];
                                                if (ids.length === 0) return 'no agents';
                                                if (ids.length <= 2) return agentNames(ids).join(', ');
                                                return `${agentNames(ids)[0]} +${ids.length - 1} more`;
                                            })()}
                                        </span>
                                    </span>
                                </button>
                                <div className="absolute right-3 top-1/2 -translate-y-1/2 flex items-center">
                                    {/* Status badge: hidden while the actions show */}
                                    <div className="group-hover:hidden group-focus-within:hidden flex-shrink-0">
                                        <StatusBadge status={ch.status} lastError={ch.last_error} />
                                    </div>
                                    {/* Quick actions: shown on hover or focus */}
                                    <div className="hidden group-hover:flex group-focus-within:flex gap-1 flex-shrink-0">
                                        <button
                                            type="button"
                                            onClick={() => toggleEnable(ch)}
                                            className={`p-1 transition-colors ${ch.status === 'running' ? 'text-orange-400 hover:text-orange-300' : 'text-green-400 hover:text-green-300'}`}
                                            aria-label={`${ch.status === 'running' ? 'Stop' : 'Start'} ${ch.name}`}
                                            title={ch.status === 'running' ? 'Stop' : 'Start'}
                                        >
                                            {ch.status === 'running' ? <Square className="h-3 w-3" aria-hidden /> : <Play className="h-3 w-3" aria-hidden />}
                                        </button>
                                        <button
                                            type="button"
                                            onClick={() => setConfirmDeleteId(ch.id || null)}
                                            className="p-1 text-text-faint hover:text-red-400 transition-colors"
                                            aria-label={`Delete ${ch.name}`}
                                            title="Delete"
                                        >
                                            <Trash className="h-3 w-3" aria-hidden />
                                        </button>
                                    </div>
                                </div>
                            </div>
                        );
                    })}
                </div>

                {/* ── Run notifications channel ─────────────────────── */}
                <div className="mt-3 pt-3 border-t border-zinc-800">
                    <label className="text-[10px] font-bold text-text-faint uppercase tracking-wider block mb-1.5">
                        Run notifications
                    </label>
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
                    <p className="text-[10px] text-text-faint mt-1 leading-relaxed">
                        Mirror orchestration notifications (needs input, completed, failed)
                        to this channel&apos;s notify chat.
                    </p>
                </div>

                {/* ── Public address for webhook-mode channels ───────── */}
                <div className="mt-3 pt-3 border-t border-zinc-800">
                    <label className="text-[10px] font-bold text-text-faint uppercase tracking-wider block mb-1.5">
                        Webhook address
                    </label>
                    <div className="flex gap-1">
                        <input
                            type="text"
                            value={webhookBaseDraft}
                            onChange={e => setWebhookBaseDraft(e.target.value)}
                            onBlur={saveWebhookBase}
                            onKeyDown={e => { if (e.key === 'Enter') saveWebhookBase(); }}
                            placeholder="https://synapse.example.com"
                            aria-label="Public webhook base URL"
                            className="flex-1 min-w-0 bg-zinc-950 border border-zinc-800 px-2 py-1.5 text-[10px] text-white font-mono focus:border-white focus:outline-none rounded-md"
                        />
                    </div>
                    <p className="text-[10px] text-text-faint mt-1 leading-relaxed">
                        Where platforms deliver messages for channels in webhook mode. A public
                        HTTPS address of this Synapse app, not its backend port — a tunnel URL works for local testing.
                    </p>
                </div>
            </div>

            {/* ── Right: Editor ──────────────────────────────────────── */}
            <div className="md:col-span-8 pl-4">
                {draft ? (
                    <div className="space-y-6">
                        {/* Header */}
                        <div className="flex items-center justify-between">
                            <h3 className="text-sm font-bold text-white flex items-center gap-2">
                                <div className="h-2 w-2 rounded-full bg-accent" />
                                {selectedId ? `EDITING: ${draft.name.toUpperCase() || 'CHANNEL'}` : 'NEW CHANNEL'}
                            </h3>
                            <div className="flex gap-2">
                                {selectedId && (
                                    <button
                                        onClick={testChannel}
                                        disabled={testing}
                                        className="flex items-center gap-1.5 px-3 py-1.5 bg-zinc-800 hover:bg-zinc-700 text-white text-xs font-bold transition-colors disabled:opacity-50"
                                    >
                                        {testing ? <Spinner size={12} /> : <FlaskConical className="h-3 w-3" />}
                                        TEST
                                    </button>
                                )}
                                <button
                                    onClick={saveAndConnect}
                                    disabled={saving}
                                    className="flex items-center gap-2 px-4 py-1.5 bg-accent text-accent-fg text-xs font-bold hover:bg-accent-hover disabled:opacity-50"
                                >
                                    {saving ? <Spinner size={12} /> : <Save className="h-3 w-3" />}
                                    SAVE & CONNECT
                                </button>
                            </div>
                        </div>

                        {/* Platform picker */}
                        <div className="space-y-2">
                            <label className="text-[10px] font-bold text-text-faint uppercase">Platform</label>
                            <div className="grid grid-cols-5 gap-2">
                                {PLATFORMS.map(p => (
                                    <button
                                        key={p.id}
                                        onClick={() => setDraft({ ...EMPTY_CHANNEL, platform: p.id, name: draft.name })}
                                        className={`flex flex-col items-center gap-1 p-3 border text-xs font-bold transition-all
                                            ${draft.platform === p.id
                                                ? `${p.color} border-current`
                                                : 'bg-black border-zinc-800 text-text-faint hover:border-zinc-600'}`}
                                    >
                                        <img src={p.icon} alt={p.label} className="h-6 w-6 object-contain" />
                                        {p.label}
                                    </button>
                                ))}
                            </div>
                        </div>

                        {/* Common fields */}
                        <div className="space-y-1">
                            <label htmlFor="messaging-channel-name" className="text-[10px] font-bold text-text-faint uppercase">Channel Name</label>
                            <input
                                id="messaging-channel-name"
                                type="text"
                                value={draft.name}
                                onChange={e => setDraft({ ...draft, name: e.target.value })}
                                placeholder="e.g. Support Bot"
                                className="w-full bg-zinc-950 border border-zinc-800 p-3 text-xs text-white focus:border-white focus:outline-none rounded-md"
                            />
                        </div>

                        {/* ── Agents on this channel ─────────────────────── */}
                        <div className="space-y-2">
                            <div>
                                <label className="text-[10px] font-bold text-text-faint uppercase">Agents</label>
                                <p className="text-[10px] text-text-faint mt-1 leading-relaxed">
                                    Who answers messages arriving here. A channel can front several
                                    agents; the one marked <strong className="text-zinc-300">default</strong> takes
                                    a plain message.
                                </p>
                            </div>
                            {!draft.id ? (
                                <p className="text-[10px] text-text-faint p-3 border border-dashed border-zinc-800 rounded-md">
                                    Save the channel first — agents are assigned to it once it exists.
                                </p>
                            ) : agents.length === 0 ? (
                                <p className="text-[10px] text-text-faint p-3 border border-dashed border-zinc-800 rounded-md">
                                    No agents configured yet. Create one under Settings → Agents.
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
                                                        className={`px-3 py-1.5 text-xs border transition-colors disabled:opacity-50 rounded-md
                                                            ${on
                                                                ? 'bg-accent text-accent-fg border-accent'
                                                                : 'bg-black border-zinc-800 text-zinc-400 hover:border-zinc-600 hover:text-white'}
                                                            ${on && !isDefault ? 'rounded-r-none' : ''}`}
                                                    >
                                                        {a.name}
                                                        {isDefault && (
                                                            <span className="ml-2 text-[9px] font-bold uppercase opacity-70">default</span>
                                                        )}
                                                    </button>
                                                    {on && !isDefault && (
                                                        <button
                                                            type="button"
                                                            onClick={() => makeDefault(a.id)}
                                                            disabled={assigning}
                                                            title={`Make ${a.name} the default agent`}
                                                            className="px-2 py-1.5 text-[9px] font-bold uppercase border border-l-0 border-accent bg-zinc-900 text-zinc-400 hover:text-white transition-colors disabled:opacity-50 rounded-r-md"
                                                        >
                                                            set default
                                                        </button>
                                                    )}
                                                </span>
                                            );
                                        })}
                                    </div>
                                    {assignedIds.length === 0 && (
                                        <p className="text-[10px] text-amber-400/80">
                                            Nobody is assigned — this channel will reply saying so instead of answering.
                                        </p>
                                    )}
                                </>
                            )}
                        </div>

                        <div className="flex items-start gap-4 p-3 bg-zinc-950 border border-zinc-800 rounded-md">
                            <input
                                id="multi-agent-toggle"
                                type="checkbox"
                                checked={draft.multi_agent_mode}
                                onChange={e => setDraft({ ...draft, multi_agent_mode: e.target.checked })}
                                className="mt-1 accent-[var(--accent)]"
                            />
                            <div className="flex-1">
                                <label htmlFor="multi-agent-toggle" className="text-xs font-bold text-white cursor-pointer block leading-tight">
                                    Multi-Agent Mode
                                </label>
                                <p className="text-[10px] text-text-faint mt-1.5 leading-relaxed">
                                    When enabled, people in the chat can switch between the agents
                                    assigned above using <code className="font-code bg-zinc-800 px-1">/agent &lt;name&gt;</code>,
                                    and list them with <code className="font-code bg-zinc-800 px-1">/agents</code>.
                                    Only assigned agents are reachable; the default answers until somebody switches.
                                </p>
                                {draft.multi_agent_mode && assignedIds.length < 2 && (
                                    <p className="text-[10px] text-amber-400/80 mt-1.5">
                                        {assignedIds.length === 1
                                            ? 'Only one agent is assigned, so there is nothing to switch between yet.'
                                            : 'No agents are assigned, so there is nothing to switch between yet.'}
                                    </p>
                                )}
                            </div>
                        </div>

                        {/* ── Delivery mode ──────────────────────────────── */}
                        {MODE_CHOICE_PLATFORMS.includes(draft.platform) && (
                            <div className="space-y-2">
                                <div>
                                    <label className="text-[10px] font-bold text-text-faint uppercase">Delivery</label>
                                    <p className="text-[10px] text-text-faint mt-1 leading-relaxed">
                                        How messages reach Synapse. Polling holds a connection open to the
                                        platform and needs no public address. Webhook holds nothing — the
                                        platform posts to you — which is what a multi-replica deployment
                                        wants, since only one process may poll a bot at a time.
                                    </p>
                                </div>
                                <div className="grid grid-cols-2 gap-3">
                                    {([
                                        { v: 'polling', label: 'Polling', desc: 'Works anywhere. One process per bot.' },
                                        { v: 'webhook', label: 'Webhook', desc: 'Needs a public HTTPS address. Survives replicas.' },
                                    ] as { v: DeliveryMode; label: string; desc: string }[]).map(opt => (
                                        <button
                                            key={opt.v}
                                            onClick={() => setDraft({ ...draft, mode: opt.v })}
                                            className={`p-3 border text-left text-xs transition-all rounded-md
                                                ${(draft.mode || 'polling') === opt.v ? 'bg-zinc-900 border-white' : 'bg-black border-zinc-800 hover:border-zinc-600'}`}
                                        >
                                            <div className="font-bold text-white">{opt.label}</div>
                                            <div className="text-text-faint mt-1">{opt.desc}</div>
                                        </button>
                                    ))}
                                </div>

                                {draft.mode === 'webhook' && (
                                    <div className="space-y-2 p-3 bg-zinc-950 border border-zinc-800 rounded-md">
                                        {!webhookBase ? (
                                            <p className="text-[10px] text-amber-400/90">
                                                Set the public webhook address below before saving — without it
                                                the channel cannot register with {platformMeta?.label}.
                                            </p>
                                        ) : !draft.id ? (
                                            <p className="text-[10px] text-text-faint">
                                                The delivery URL appears here once the channel is saved.
                                            </p>
                                        ) : (
                                            <div className="space-y-1">
                                                <label htmlFor="messaging-delivery-url" className="text-[10px] font-bold text-text-faint uppercase">Delivery URL</label>
                                                <input
                                                    id="messaging-delivery-url"
                                                    readOnly
                                                    value={webhookUrl}
                                                    onFocus={e => e.currentTarget.select()}
                                                    className="w-full bg-black border border-zinc-800 p-2.5 text-[10px] text-zinc-300 font-mono rounded-md"
                                                />
                                                <p className="text-[9px] text-text-faint">
                                                    {draft.platform === 'telegram'
                                                        ? 'Registered with Telegram automatically on Save & Connect.'
                                                        : `Paste this into your ${platformMeta?.label} app configuration.`}
                                                </p>
                                            </div>
                                        )}
                                        {draft.platform === 'slack' && (
                                            <p className="text-[9px] text-text-faint">
                                                Webhook mode does not need the app-level token (xapp-…) — that is
                                                Socket Mode&apos;s, and Socket Mode is the polling option.
                                            </p>
                                        )}
                                    </div>
                                )}
                            </div>
                        )}

                        {/* Platform-specific credentials */}
                        <div className="space-y-3">
                            <label className="text-[10px] font-bold text-text-faint uppercase">Credentials — {platformMeta?.label}</label>

                            {/* WhatsApp special UI */}
                            {draft.platform === 'whatsapp' ? (
                                <div className="space-y-4">
                                    {/* Mode selector */}
                                    <div className="grid grid-cols-2 gap-3">
                                        {[
                                            { v: 'meta_api', label: '🏢 Business (Meta API)', desc: 'Official. Requires Meta Business account + verified number.' },
                                            { v: 'unofficial', label: '🛠 Unofficial (Playwright)', desc: 'No business account. Uses WhatsApp Web automation.' },
                                        ].map(opt => (
                                            <button
                                                key={opt.v}
                                                onClick={() => { setWaMode(opt.v as any); setWaRiskAck(false); }}
                                                className={`p-3 border text-left text-xs transition-all
                                                    ${waMode === opt.v ? 'bg-zinc-900 border-white' : 'bg-black border-zinc-800 hover:border-zinc-600'} rounded-md`}
                                            >
                                                <div className="font-bold text-white">{opt.label}</div>
                                                <div className="text-text-faint mt-1">{opt.desc}</div>
                                            </button>
                                        ))}
                                    </div>

                                    {waMode === 'meta_api' && (
                                        <div className="space-y-3">
                                            {[
                                                { key: 'phone_number_id', label: 'Phone Number ID', placeholder: '123456789012345', secret: false },
                                                { key: 'access_token', label: 'Access Token', placeholder: 'EAA...', secret: true },
                                                { key: 'app_secret', label: 'App Secret', placeholder: '', secret: true,
                                                  note: 'App settings → Basic. Meta signs every delivery with it; a delivery without a matching signature is refused.' },
                                                { key: 'verify_token', label: 'Verify Token (your choice)', placeholder: 'synapse_verify', secret: false,
                                                  note: 'Used to verify Meta webhook. Set the same value in your Meta App settings.' },
                                            ].map(f => (
                                                <div key={f.key} className="space-y-1">
                                                    <label htmlFor={`messaging-cred-${f.key}`} className="text-[10px] font-bold text-text-faint uppercase">{f.label}</label>
                                                    <input
                                                        id={`messaging-cred-${f.key}`}
                                                        type={f.secret ? 'password' : 'text'}
                                                        value={draft.credentials[f.key] || ''}
                                                        onChange={e => updateCred(f.key, e.target.value)}
                                                        placeholder={credPlaceholder(draft, f.key, f.secret, f.placeholder)}
                                                        className="w-full bg-zinc-950 border border-zinc-800 p-3 text-xs text-white focus:border-white focus:outline-none font-mono rounded-md"
                                                    />
                                                    {f.note && <p className="text-[9px] text-text-faint">{f.note}</p>}
                                                </div>
                                            ))}
                                        </div>
                                    )}

                                    {waMode === 'unofficial' && (
                                        <div className="space-y-3">
                                            <div className="flex gap-2 p-3 bg-amber-950/30 border border-amber-800/50 rounded-md">
                                                <AlertTriangle className="h-4 w-4 text-amber-400 flex-shrink-0 mt-0.5" />
                                                <div className="text-[10px] text-amber-300 space-y-1">
                                                    <p className="font-bold">⚠️ Unofficial Approach — Use at Your Own Risk</p>
                                                    <p>This mode uses Playwright to drive WhatsApp Web in a browser window. It requires scanning a QR code. It may:</p>
                                                    <ul className="list-disc pl-4 space-y-0.5">
                                                        <li>Violate WhatsApp's Terms of Service</li>
                                                        <li>Break when WhatsApp updates their web app</li>
                                                        <li>Result in your number being temporarily banned</li>
                                                    </ul>
                                                    <p>We recommend using the official Meta Business path instead.</p>
                                                </div>
                                            </div>
                                            <div className="flex items-start gap-3 p-3 bg-zinc-950 border border-zinc-800 rounded-md">
                                                <input
                                                    id="wa-risk-ack"
                                                    type="checkbox"
                                                    checked={waRiskAck}
                                                    onChange={e => setWaRiskAck(e.target.checked)}
                                                    className="mt-1 accent-amber-500"
                                                />
                                                <label htmlFor="wa-risk-ack" className="text-xs text-zinc-300 cursor-pointer leading-tight">
                                                    I understand the risks and accept responsibility for using the unofficial path.
                                                </label>
                                            </div>
                                            {waRiskAck && (
                                                <div className="p-3 bg-zinc-950 border border-zinc-700 text-[10px] text-zinc-400 space-y-1 rounded-md">
                                                    <p>After saving & connecting, Synapse will open WhatsApp Web in a Chromium browser window.</p>
                                                    <p>Scan the QR code shown in the browser to authenticate. The session is saved and persists across restarts.</p>
                                                </div>
                                            )}
                                        </div>
                                    )}
                                </div>
                            ) : (
                                /* Standard credential fields */
                                <div className="space-y-3">
                                    {CREDENTIAL_FIELDS[draft.platform]?.filter(f => !f.mode || f.mode === (draft.mode || 'polling')).map(f => (
                                        <div key={f.key} className="space-y-1">
                                            <label htmlFor={`messaging-cred-${f.key}`} className="text-[10px] font-bold text-text-faint uppercase">{f.label}</label>
                                            <input
                                                id={`messaging-cred-${f.key}`}
                                                type={f.secret ? 'password' : 'text'}
                                                value={draft.credentials[f.key] || ''}
                                                onChange={e => updateCred(f.key, e.target.value)}
                                                placeholder={credPlaceholder(draft, f.key, f.secret, f.placeholder)}
                                                className="w-full bg-zinc-950 border border-zinc-800 p-3 text-xs text-white focus:border-white focus:outline-none font-mono rounded-md"
                                            />
                                            {f.note && (
                                                <p className="text-[9px] text-text-faint flex items-center gap-1">
                                                    <Info className="h-2.5 w-2.5" /> {f.note}
                                                </p>
                                            )}
                                        </div>
                                    ))}
                                </div>
                            )}
                        </div>

                        {/* Setup Guide accordion */}
                        <div className="border border-zinc-800">
                            <button
                                onClick={() => setGuideOpen(g => !g)}
                                className="w-full flex items-center justify-between px-4 py-3 text-xs font-bold text-zinc-400 hover:text-white hover:bg-zinc-900/50 transition-colors"
                            >
                                <span>📖 Setup Guide — {guide.title}</span>
                                {guideOpen ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
                            </button>
                            {guideOpen && (
                                <div className="px-4 pb-4 space-y-2 bg-zinc-950/50">
                                    <ol className="space-y-2">
                                        {guide.steps.map((s, i) => (
                                            <li key={i} className="flex gap-2 text-[10px] text-zinc-400">
                                                <span className="text-text-faint font-bold flex-shrink-0">{s.startsWith('---') ? '' : `${i + 1}.`}</span>
                                                <span className={s.startsWith('---') ? 'text-text-faint border-t border-zinc-800 w-full pt-1' : ''}>{s}</span>
                                            </li>
                                        ))}
                                    </ol>
                                </div>
                            )}
                        </div>

                        {/* Error display */}
                        {draft.last_error && (
                            <div className="flex gap-2 p-3 bg-red-950/30 border border-red-900/50 rounded-md">
                                <XCircle className="h-4 w-4 text-red-400 flex-shrink-0" />
                                <div>
                                    <p className="text-xs font-bold text-red-400">Last Error</p>
                                    <p className="text-[10px] text-red-300 mt-0.5 font-mono">{draft.last_error}</p>
                                </div>
                            </div>
                        )}
                    </div>
                ) : (
                    <div className="h-full flex flex-col items-center justify-center text-text-faint space-y-4 py-20">
                        <MessageSquare className="h-12 w-12 opacity-20" />
                        <p className="text-sm">Select a channel or create a new one.</p>
                    </div>
                )}
            </div>

            <ConfirmationModal
                isOpen={!!confirmDeleteId}
                title="Delete Channel"
                message="Are you sure you want to delete this messaging channel? This will permanently remove its configuration and stop it if it is running."
                onConfirm={() => {
                    if (confirmDeleteId) deleteChannel(confirmDeleteId);
                    setConfirmDeleteId(null);
                }}
                onClose={() => setConfirmDeleteId(null)}
            />
        </div>
    );
};
