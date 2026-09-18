/* eslint-disable @typescript-eslint/no-explicit-any */
import { useEffect, useRef, useState } from 'react';
import {
    Server, Plus, Trash, RefreshCw, CheckCircle, XCircle, AlertCircle, Zap,
    Terminal, Globe, Eye, EyeOff, ShieldAlert
} from 'lucide-react';
import { useDispatch } from 'react-redux';
import { AppDispatch } from '@/store';
import { setMcpServers } from '@/store/settingsSlice';
import { Label, Spinner, type ToastTone } from '@/components/ui';

// ── Types ──────────────────────────────────────────────────────────────────────

/*
 * The inline notice above the server list.
 *
 * Deliberately NOT the kit's floating `Toast`. "OAuth required — return here
 * once authorised" is an instruction about this page, and a message that
 * appears bottom-right and fades after four seconds is the wrong place for
 * something the user has to come back and act on. Tone comes from the kit so
 * there is still only one set of names for these three states.
 */
export interface McpNotice {
    message: string;
    tone: ToastTone;
}

type DraftServer = {
    name: string;
    label: string;
    server_type: 'stdio' | 'remote';
    command: string;
    args: string;
    env: { key: string; value: string }[];
    url: string;
    token: string;
};

interface McpServersTabProps {
    mcpServers: any[];
    loadingMcp: boolean;
    isConnecting: boolean;
    lastConnected: boolean | null;
    notice: McpNotice | null;
    onNotice: (message: string, tone?: ToastTone) => void;
    pendingServerName: string | null;      // remote server awaiting OAuth
    onPendingResolved: () => void;         // call when connected or timed-out
    draftMcpServer: DraftServer;
    setDraftMcpServer: (v: DraftServer) => void;
    onAddServer: () => void;
    onDeleteServer: (name: string) => void;
    onReconnectServer: (name: string) => void;
}

// ── Presets ────────────────────────────────────────────────────────────────────

interface Preset {
    name: string;
    server_type: 'stdio' | 'remote';
    label: string;
    // stdio
    command?: string;
    args?: string;
    env?: Record<string, string>;
    // remote
    url?: string;
    /**
     * How the server authenticates. A preset never fills the token: a
     * non-empty token skips OAuth, and these used to pre-fill the *names* of
     * environment variables (SLACK_CLIENT_ID, FIGMA_PERSONAL_ACCESS_TOKEN), so
     * Slack and Figma — both OAuth — went out with a bearer token that was not
     * one (F74). Taken from the commercial edition's catalog, which was probed.
     */
    auth?: 'oauth' | 'bearer' | 'none';
    /** For `bearer`: what token, and where to make one. */
    tokenHint?: string;
    tokenUrl?: string;
}

const STDIO_PRESETS: Preset[] = [
    { server_type: 'stdio', name: 'Git', command: 'uvx', args: 'mcp-server-git', label: 'Git' }
];

const REMOTE_PRESETS: Preset[] = [
    { server_type: 'remote', name: 'Vercel', url: 'https://mcp.vercel.com', label: 'Vercel', auth: 'oauth' },
    {
        server_type: 'remote', name: 'Github', url: 'https://api.githubcopilot.com/mcp/', label: 'GitHub Copilot', auth: 'bearer',
        tokenHint: 'A GitHub personal access token with access to the repositories you want reachable.',
        tokenUrl: 'https://github.com/settings/personal-access-tokens',
    },
    { server_type: 'remote', name: 'slack', url: 'https://mcp.slack.com/mcp', label: 'Slack', auth: 'oauth' },
    { server_type: 'remote', name: 'notion', url: 'https://mcp.notion.com/mcp', label: 'Notion', auth: 'oauth' },
    { server_type: 'remote', name: 'Jira', url: 'https://mcp.atlassian.com/v1/mcp', label: 'Jira', auth: 'oauth' },
    { server_type: 'remote', name: 'Zapier', url: 'https://mcp.zapier.com/api/mcp/mcp', label: 'Zapier' },
    { server_type: 'remote', name: 'Figma', url: 'https://mcp.figma.com/mcp', label: 'Figma', auth: 'oauth' },
    { server_type: 'remote', name: 'Fetch', url: 'https://remote.mcpservers.org/fetch/mcp', label: 'Fetch', auth: 'none' },
];

// ── Sub-components ─────────────────────────────────────────────────────────────

const StatusBadge = ({ status }: { status?: string }) => {
    if (status === 'connecting') return (
        <span className="flex items-center gap-1 text-2xs bg-accent/20 text-accent px-1.5 py-0.5 rounded-md border border-accent/30 uppercase">
            <Spinner size={10} /> Connecting
        </span>
    );
    if (status === 'connected') return (
        <span className="flex items-center gap-1 text-2xs bg-success/20 text-success px-1.5 py-0.5 rounded-md border border-success/30 uppercase">
            <CheckCircle className="h-2.5 w-2.5" /> Active
        </span>
    );
    if (status === 'reauth_needed') return (
        <span className="flex items-center gap-1 text-2xs bg-warning/20 text-warning px-1.5 py-0.5 rounded-md border border-warning/30 uppercase">
            <ShieldAlert className="h-2.5 w-2.5" /> Re-Auth
        </span>
    );
    return (
        <span className="flex items-center gap-1 text-2xs bg-warning/20 text-warning px-1.5 py-0.5 rounded-md border border-warning/30 uppercase">
            <XCircle className="h-2.5 w-2.5" /> Disconnected
        </span>
    );
};

const TypePill = ({ type }: { type?: string }) => (
    type === 'remote'
        ? <span className="flex items-center gap-1 text-2xs bg-accent/15 text-accent px-1.5 py-0.5 rounded-md border border-accent/25 uppercase"><Globe className="h-2 w-2" />Remote</span>
        : <span className="flex items-center gap-1 text-2xs bg-surface-2 text-text-faint px-1.5 py-0.5 rounded-md border border-border-strong uppercase"><Terminal className="h-2 w-2" />Local</span>
);

const NOTICE_TONES: Record<ToastTone, { chip: string; icon: React.ElementType }> = {
    success: { chip: 'bg-success-subtle border-success/40 text-success', icon: CheckCircle },
    warning: { chip: 'bg-warning-subtle border-warning/40 text-warning', icon: AlertCircle },
    danger: { chip: 'bg-danger-subtle border-danger/40 text-danger', icon: XCircle },
};

const inputCls = "w-full bg-surface border border-border p-2 text-sm text-text focus:border-border-strong focus:outline-none placeholder:text-text-faint";
const monoInputCls = `${inputCls} font-mono`;

/**
 * A stable colour per server, hashed from its name — the gallery-tile idea:
 * saturation and lightness fixed, hue the only free variable, so a column of
 * marks reads as one family with the hue doing the distinguishing.
 */
function tileHue(name: string): number {
    let hash = 0;
    for (let i = 0; i < name.length; i += 1) hash = (hash * 31 + name.charCodeAt(i)) % 360;
    return hash;
}

function ServerMark({ label }: { label: string }) {
    const hue = tileHue(label.toLowerCase());
    return (
        <span
            aria-hidden
            className="flex h-9 w-9 shrink-0 items-center justify-center rounded-md text-sm font-semibold"
            style={{ backgroundColor: `hsl(${hue} 45% 22%)`, color: `hsl(${hue} 70% 72%)` }}
        >
            {label.charAt(0).toUpperCase()}
        </span>
    );
}

// ── Main component ─────────────────────────────────────────────────────────────

export const McpServersTab = ({
    mcpServers, loadingMcp, isConnecting,
    notice, onNotice,
    pendingServerName, onPendingResolved,
    draftMcpServer, setDraftMcpServer,
    onAddServer, onDeleteServer, onReconnectServer,
}: McpServersTabProps) => {
    const dispatch = useDispatch<AppDispatch>();

    // Track which form panel is active (controlled by draftMcpServer.server_type)
    const serverType = draftMcpServer.server_type;
    const setServerType = (t: 'stdio' | 'remote') =>
        setDraftMcpServer({ ...draftMcpServer, server_type: t });

    // ── Refresh ────────────────────────────────────────────────────────────────
    const [isRefreshing, setIsRefreshing] = useState(false);
    const [tokenVisible, setTokenVisible] = useState(true);
    // The preset last applied, while its URL is still the one in the form.
    const [appliedPreset, setAppliedPreset] = useState<Preset | null>(null);

    const refreshServers = async (silent = false) => {
        if (!silent) setIsRefreshing(true);
        try {
            const res = await fetch('/api/mcp/servers');
            if (res.ok) {
                const servers = await res.json();
                dispatch(setMcpServers(Array.isArray(servers) ? servers : []));
            }
        } catch { /* silent */ } finally {
            if (!silent) setIsRefreshing(false);
        }
    };

    // Background status sync — catches reauth/disconnect signals set by backend
    // during tool execution without the user needing to manually refresh.
    useEffect(() => {
        const id = setInterval(() => refreshServers(true), 30_000);
        return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);

    // ── Polling for pending OAuth remote server ────────────────────────────
    // Active only when this tab is visible and pendingServerName is set.
    // Polls /api/mcp/servers every 5 s for up to 60 s; stops on connected.
    const [, setPollCountdown] = useState(0);
    const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
    const tickRef = useRef<ReturnType<typeof setInterval> | null>(null);
    const pollEndRef = useRef(0);

    const stopPolling = () => {
        if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
        if (tickRef.current) { clearInterval(tickRef.current); tickRef.current = null; }
        setPollCountdown(0);
    };

    useEffect(() => {
        if (!pendingServerName) { stopPolling(); return; }

        pollEndRef.current = Date.now() + 60_000;
        setPollCountdown(60);

        pollRef.current = setInterval(async () => {
            try {
                const res = await fetch('/api/mcp/servers');
                if (!res.ok) return;
                const servers: any[] = await res.json();
                dispatch(setMcpServers(servers));
                const target = servers.find((s: any) => s.name === pendingServerName);
                if (target?.status === 'connected') {
                    stopPolling();
                    onPendingResolved();
                    onNotice(`${pendingServerName} connected`);
                    return;
                }
            } catch { /* ignore */ }
            if (Date.now() >= pollEndRef.current) {
                stopPolling();
                onPendingResolved();
            }
        }, 5_000);

        tickRef.current = setInterval(() => {
            const remaining = Math.max(0, Math.round((pollEndRef.current - Date.now()) / 1000));
            setPollCountdown(remaining);
            if (remaining <= 0) { stopPolling(); onPendingResolved(); }
        }, 1_000);

        return stopPolling;
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [pendingServerName]);

    // ── Preset helper ──────────────────────────────────────────────────────────
    const applyPreset = (p: Preset) => {
        const env = p.env
            ? Object.entries(p.env).map(([key, value]) => ({ key, value }))
            : [];
        setDraftMcpServer({
            name: p.name,
            label: p.label,
            server_type: p.server_type,
            command: p.command || '',
            args: p.args || '',
            env,
            url: p.url || '',
            token: '',
        });
        setAppliedPreset(p);
    };

    // ── Env var helpers ────────────────────────────────────────────────────────
    const addEnvVar = () => setDraftMcpServer({ ...draftMcpServer, env: [...draftMcpServer.env, { key: '', value: '' }] });
    const removeEnvVar = (i: number) => setDraftMcpServer({ ...draftMcpServer, env: draftMcpServer.env.filter((_, idx) => idx !== i) });
    const updateEnvVar = (i: number, field: 'key' | 'value', val: string) => {
        const newEnv = [...draftMcpServer.env];
        newEnv[i] = { ...newEnv[i], [field]: val };
        setDraftMcpServer({ ...draftMcpServer, env: newEnv });
    };

    // ── Render ─────────────────────────────────────────────────────────────────
    return (
        <div className="space-y-8">

            {/* ── Header ── */}
            <div className="flex items-start justify-between gap-4">
                <div>
                    <h3 className="text-lg font-bold text-text flex items-center gap-2">
                        <Server className="h-5 w-5" /> External MCP Servers
                    </h3>
                    <p className="text-text-faint text-sm mt-1">
                        Connect local and remote Model Context Protocol servers to extend agent capabilities.
                    </p>
                </div>
                <button
                    onClick={() => refreshServers()}
                    disabled={isRefreshing}
                    title="Refresh server statuses"
                    className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium border border-border-strong text-text-muted hover:border-text-faint hover:text-text rounded-md transition-colors disabled:opacity-50 shrink-0 mt-1"
                >
                    <RefreshCw className={`h-3.5 w-3.5 ${isRefreshing ? 'animate-spin' : ''}`} />
                    {isRefreshing ? 'Refreshing…' : 'Refresh'}
                </button>
            </div>

            {/* ── Inline Toast ── */}
            {notice && (
                <div className={`flex items-start gap-2.5 px-4 py-3 rounded-md border text-xs font-medium animate-in fade-in slide-in-from-top-2 duration-200 ${NOTICE_TONES[notice.tone].chip}`}>
                    {(() => { const Icon = NOTICE_TONES[notice.tone].icon; return <Icon className="h-4 w-4 mt-0.5 shrink-0" />; })()}
                    <span className="leading-relaxed">{notice.message}</span>
                </div>
            )}

            {/* ── Connected Servers List ── */}
            <div className="space-y-4">
                <h4 className="text-xs uppercase font-bold text-text-faint tracking-wider">Connected Servers</h4>
                {loadingMcp ? (
                    <div className="flex items-center gap-2 text-text-faint text-sm">
                        <Spinner size={16} /> Loading…
                    </div>
                ) : mcpServers.length === 0 ? (
                    <div className="p-8 text-center border border-dashed border-border rounded-md bg-surface/30">
                        <Server className="h-8 w-8 mx-auto text-text-faint mb-2" />
                        <p className="text-text-faint text-sm">No servers added yet.</p>
                        <p className="text-text-faint text-xs mt-1">Pick a preset or fill the form below.</p>
                    </div>
                ) : (
                    <div className="grid gap-3">
                        {[...mcpServers]
                            .sort((a, b) => (a.label || a.name).localeCompare(b.label || b.name, undefined, { sensitivity: 'base' }))
                            .map((server) => (
                            <div key={server.name} className="flex items-center justify-between p-4 bg-surface border border-border rounded-md group">
                                <div className="flex items-center gap-3 min-w-0">
                                <ServerMark label={server.label || server.name} />
                                <div className="flex flex-col gap-1.5 min-w-0">
                                    <div className="flex items-center gap-2 flex-wrap">
                                        <span className="font-bold text-text text-sm">{server.label || server.name}</span>
                                        {server.label && server.label !== server.name && (
                                            <span className="text-2xs text-text-faint font-mono">{server.name}</span>
                                        )}
                                        <TypePill type={server.server_type} />
                                        <StatusBadge status={server.status} />
                                    </div>
                                    <code className="text-2xs text-text-faint font-code truncate">
                                        {server.server_type === 'remote'
                                            ? server.url
                                            : `${server.command} ${(server.args || []).join(' ')}`}
                                    </code>
                                </div>
                                </div>
                                <div className="flex items-center gap-1 ml-4 shrink-0">
                                    {server.status === 'connecting' && (
                                        <span className="p-2"><Spinner size={14} className="text-accent" /></span>
                                    )}
                                    {server.status === 'reauth_needed' && (
                                        <button onClick={() => onReconnectServer(server.name)} title="Re-authenticate"
                                            className="p-2 text-warning hover:bg-surface-2 rounded-md transition-colors">
                                            <ShieldAlert className="h-3.5 w-3.5" />
                                        </button>
                                    )}
                                    {(!server.status || server.status === 'disconnected') && (
                                        <button onClick={() => onReconnectServer(server.name)} title="Retry connection"
                                            className="p-2 text-text-faint hover:text-accent hover:bg-surface-2 rounded-md transition-colors">
                                            <RefreshCw className="h-3.5 w-3.5" />
                                        </button>
                                    )}
                                    {server.status === 'connected' && (
                                        <button onClick={() => onReconnectServer(server.name)} title="Force reconnect (refresh stale session)"
                                            className="p-2 text-text-faint hover:text-text hover:bg-surface-2 rounded-md opacity-0 group-hover:opacity-100 transition-all">
                                            <RefreshCw className="h-3.5 w-3.5" />
                                        </button>
                                    )}
                                    <button onClick={() => onDeleteServer(server.name)}
                                        aria-label={`Remove ${server.label || server.name}`}
                                        title={`Remove ${server.label || server.name}`}
                                        className="p-2 text-text-faint hover:text-danger hover:bg-surface-2 rounded-md transition-colors">
                                        <Trash className="h-4 w-4" />
                                    </button>
                                </div>
                            </div>
                        ))}
                    </div>
                )}
            </div>

            {/* ── Add Server Form ── */}
            <div className="pt-6 border-t border-border space-y-6">

                {/* Type toggle */}
                <div className="flex items-center gap-1 bg-surface border border-border p-1 w-fit rounded-md">
                    <button
                        onClick={() => setServerType('stdio')}
                        className={`flex items-center gap-1.5 px-4 py-1.5 text-xs font-bold transition-colors ${serverType === 'stdio' ? 'bg-accent text-accent-fg' : 'text-text-faint hover:text-text'}`}
                    >
                        <Terminal className="h-3 w-3" /> Local (stdio)
                    </button>
                    <button
                        onClick={() => setServerType('remote')}
                        className={`flex items-center gap-1.5 px-4 py-1.5 text-xs font-bold transition-colors ${serverType === 'remote' ? 'bg-accent text-accent-fg' : 'text-text-faint hover:text-text'}`}
                    >
                        <Globe className="h-3 w-3" /> Remote (URL)
                    </button>
                </div>

                {/* ── Presets ── */}
                <div className="space-y-3">
                    <div className="flex items-center gap-2">
                        <Zap className="h-3.5 w-3.5 text-text-faint" />
                        <h4 className="text-xs uppercase font-bold text-text-faint tracking-wider">
                            {serverType === 'stdio' ? 'Local Presets' : 'Remote Presets'}
                        </h4>
                    </div>
                    <div className="flex flex-wrap gap-2">
                        {(serverType === 'stdio' ? STDIO_PRESETS : REMOTE_PRESETS).map(p => (
                            <button key={p.name + p.label} onClick={() => applyPreset(p)}
                                className="px-3 py-1.5 text-2xs font-medium bg-surface border border-border text-text-muted hover:border-text-faint hover:text-text rounded-md transition-colors">
                                {p.label}
                            </button>
                        ))}
                    </div>
                    <p className="text-2xs text-text-faint">
                        Find more on the{' '}
                        <a href="https://github.com/modelcontextprotocol/servers" target="_blank" rel="noopener noreferrer"
                            className="text-text-muted underline underline-offset-2 hover:text-text transition-colors">
                            MCP servers registry
                        </a>.
                        {serverType === 'remote' && ' Remote servers use native OAuth — no npx required.'}
                    </p>
                </div>

                {/* ── Fields ── */}
                <div className="space-y-4">
                    {/* Display Label + Unique ID — always shown */}
                    <div className="grid grid-cols-2 gap-4">
                        <div className="space-y-2">
                            <Label htmlFor="mcp-draftmcpserver-label" className="block">Display Label</Label>
                            <input id="mcp-draftmcpserver-label" type="text" value={draftMcpServer.label}
                                onChange={e => setDraftMcpServer({ ...draftMcpServer, label: e.target.value })}
                                className={inputCls} placeholder="e.g. GitHub Production" />
                        </div>
                        <div className="space-y-2">
                            <Label htmlFor="mcp-draftmcpserver-name" className="block">Unique ID</Label>
                            <input id="mcp-draftmcpserver-name" type="text" value={draftMcpServer.name}
                                onChange={e => setDraftMcpServer({ ...draftMcpServer, name: e.target.value })}
                                className={inputCls} placeholder="e.g. github-prod" />
                        </div>
                    </div>

                    {serverType === 'stdio' ? (
                        /* ── stdio fields ── */
                        <>
                            <div className="grid grid-cols-2 gap-4">
                                <div className="space-y-2">
                                    <Label htmlFor="mcp-draftmcpserver-command" className="block">Command</Label>
                                    <input id="mcp-draftmcpserver-command" type="text" value={draftMcpServer.command}
                                        onChange={e => setDraftMcpServer({ ...draftMcpServer, command: e.target.value })}
                                        className={monoInputCls} placeholder="npx, uvx, python3" />
                                </div>
                                <div className="space-y-2">
                                    <Label htmlFor="mcp-draftmcpserver-args" className="block">Arguments</Label>
                                    <input id="mcp-draftmcpserver-args" type="text" value={draftMcpServer.args}
                                        onChange={e => setDraftMcpServer({ ...draftMcpServer, args: e.target.value })}
                                        className={monoInputCls} placeholder="-y @org/server-name" />
                                </div>
                            </div>

                            {/* Env vars */}
                            <div className="space-y-2">
                                <div className="flex items-center justify-between">
                                    <Label className="block">Environment Variables</Label>
                                    <button onClick={addEnvVar}
                                        className="text-2xs font-bold text-text-muted hover:text-text flex items-center gap-1">
                                        <Plus className="h-3 w-3" /> ADD VAR
                                    </button>
                                </div>
                                {draftMcpServer.env.map((env, i) => (
                                    <div key={i} className="flex gap-2">
                                        <input type="text" aria-label={`Variable ${i + 1} name`} placeholder="KEY" value={env.key}
                                            onChange={e => updateEnvVar(i, 'key', e.target.value)}
                                            className="flex-1 bg-surface border border-border p-2 text-xs text-text font-mono focus:border-border-strong focus:outline-none rounded-md" />
                                        <input type="text" aria-label={`Variable ${i + 1} value`} placeholder="VALUE" value={env.value}
                                            onChange={e => updateEnvVar(i, 'value', e.target.value)}
                                            className="flex-[2] bg-surface border border-border p-2 text-xs text-text font-mono focus:border-border-strong focus:outline-none rounded-md" />
                                        <button onClick={() => removeEnvVar(i)} aria-label={`Remove variable ${i + 1}`} className="p-2 text-text-faint hover:text-danger">
                                            <Trash className="h-4 w-4" />
                                        </button>
                                    </div>
                                ))}
                            </div>
                        </>
                    ) : (
                        /* ── remote fields ── */
                        <>
                            <div className="space-y-2">
                                <Label htmlFor="mcp-draftmcpserver-url" className="block">Server URL</Label>
                                <input id="mcp-draftmcpserver-url" type="url" value={draftMcpServer.url}
                                    onChange={e => setDraftMcpServer({ ...draftMcpServer, url: e.target.value })}
                                    className={monoInputCls} placeholder="https://mcp.example.com/mcp" />
                                <p className="text-2xs text-text-faint">
                                    Leave the token empty to sign in with OAuth (a browser window opens). Fill it only for a
                                    server that takes a personal access token.
                                </p>
                            </div>
                            <div className="space-y-2">
                                <Label htmlFor="mcp-token" className="block">
                                    Bearer Token / Personal Access Token{' '}
                                    <span className="normal-case font-normal">(optional — leave empty for OAuth)</span>
                                </Label>
                                <div className="relative">
                                    <input
                                        id="mcp-token"
                                        type={tokenVisible ? 'text' : 'password'}
                                        value={draftMcpServer.token}
                                        onChange={e => setDraftMcpServer({ ...draftMcpServer, token: e.target.value })}
                                        className={`${monoInputCls} pr-10`}
                                        placeholder="ghp_... or leave empty"
                                    />
                                    <button
                                        type="button"
                                        onClick={() => setTokenVisible(v => !v)}
                                        title={tokenVisible ? 'Hide token' : 'Show token'}
                                        className="absolute right-2 top-1/2 -translate-y-1/2 text-text-faint hover:text-text transition-colors"
                                    >
                                        {tokenVisible
                                            ? <EyeOff className="h-4 w-4" />
                                            : <Eye className="h-4 w-4" />}
                                    </button>
                                </div>
                                {appliedPreset && appliedPreset.url === draftMcpServer.url && appliedPreset.auth && (
                                    <p className="text-2xs text-text-faint leading-relaxed">
                                        {appliedPreset.auth === 'oauth' && <>
                                            {appliedPreset.label} signs you in with OAuth: leave the token empty, and a browser
                                            window opens when you connect.
                                        </>}
                                        {appliedPreset.auth === 'bearer' && <>
                                            {appliedPreset.label} needs a token. {appliedPreset.tokenHint}{' '}
                                            {appliedPreset.tokenUrl && (
                                                <a href={appliedPreset.tokenUrl} target="_blank" rel="noopener noreferrer"
                                                    className="text-accent underline underline-offset-2">Create one</a>
                                            )}
                                        </>}
                                        {appliedPreset.auth === 'none' && <>{appliedPreset.label} needs no account.</>}
                                    </p>
                                )}
                            </div>
                        </>
                    )}
                </div>

                {/* Submit */}
                <div className="flex justify-end pt-2">
                    <button onClick={onAddServer} disabled={isConnecting}
                        className="flex items-center gap-2 px-6 py-2 bg-accent text-accent-fg text-sm font-bold hover:bg-accent-hover transition-colors disabled:opacity-60 disabled:cursor-not-allowed">
                        {isConnecting
                            ? <><Spinner size={16} /> Connecting…</>
                            : 'Connect Server'}
                    </button>
                </div>
            </div>
        </div>
    );
};
