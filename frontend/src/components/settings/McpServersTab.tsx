/* eslint-disable @typescript-eslint/no-explicit-any */
import { useEffect, useRef, useState } from 'react';
import {
    Server, Plus, Trash, RefreshCw, CheckCircle, XCircle, AlertCircle, Zap,
    Terminal, Globe, Eye, EyeOff, ShieldAlert, UserRound
} from 'lucide-react';
import { useDispatch } from 'react-redux';
import { AppDispatch } from '@/store';
import { setMcpServers } from '@/store/settingsSlice';
import { AppScreen } from '@/components/app/AppScreen';
import { GoogleWorkspaceCard } from './GoogleWorkspaceCard';
import { AppMark, Badge, Button, Label, Spinner, type ToastTone } from '@/components/ui';

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
    /** What sits under the form: the stdio policy, the remove dialog. */
    children?: React.ReactNode;
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

/**
 * Tokens the old Slack, Figma and GitHub presets filled in — environment
 * variable *names*, not tokens (F103). A non-empty token skips OAuth, so a
 * server saved with one never signs in; F74 fixed the presets, not the servers
 * already saved, and nothing stored is rewritten. The list route names the
 * placeholder (`token_placeholder`) and never serves a token itself (F135). The
 * value says what the preset needs now.
 */
const PLACEHOLDER_TOKENS: Record<string, { preset: string; needs: 'oauth' | 'token' }> = {
    SLACK_CLIENT_ID: { preset: 'Slack', needs: 'oauth' },
    FIGMA_PERSONAL_ACCESS_TOKEN: { preset: 'Figma', needs: 'oauth' },
    GITHUB_PERSONAL_ACCESS_TOKEN: { preset: 'GitHub Copilot', needs: 'token' },
};

// ── Sub-components ─────────────────────────────────────────────────────────────

/*
 * A server's state as a dot and words (M2f). Connected is the one that earns a
 * colour; a sign-in that is needed is a person's job, so amber; connecting is
 * the product acting; disconnected is quiet.
 */
const ServerStatus = ({ status }: { status?: string }) => {
    if (status === 'connecting') return (
        <span className="inline-flex items-center gap-1.5 text-xs font-medium text-accent-ink">
            <Spinner size={12} className="text-machine" /> Connecting
        </span>
    );
    if (status === 'connected') return (
        <span className="inline-flex items-center gap-1.5 text-xs font-medium text-success">
            <span aria-hidden className="size-2 rounded-full bg-success" /> Connected
        </span>
    );
    if (status === 'reauth_needed') return (
        <span className="inline-flex items-center gap-1.5 text-xs font-medium text-person-ink">
            <ShieldAlert className="size-3.5" aria-hidden /> Re-Auth
        </span>
    );
    return (
        <span className="inline-flex items-center gap-1.5 text-xs font-medium text-text-faint">
            <span aria-hidden className="size-2 rounded-full border border-text-faint" /> Disconnected
        </span>
    );
};

const TypePill = ({ type }: { type?: string }) => (
    <Badge size="sm">
        {type === 'remote'
            ? <span className="inline-flex items-center gap-1"><Globe className="size-3" aria-hidden />Remote</span>
            : <span className="inline-flex items-center gap-1"><Terminal className="size-3" aria-hidden />Local</span>}
    </Badge>
);

// The kit Toast's tones, in lockstep (components/ui/toast.tsx): the tone is the
// icon's, and only a failure is coloured through.
const NOTICE_TONES: Record<ToastTone, { chip: string; icon: React.ElementType; mark: string }> = {
    success: { chip: 'bg-surface border-border text-text', icon: CheckCircle, mark: 'text-success' },
    warning: { chip: 'bg-surface border-border text-text', icon: AlertCircle, mark: 'text-text' },
    danger: { chip: 'bg-danger-subtle border-danger/40 text-danger', icon: XCircle, mark: 'text-danger' },
    person: { chip: 'bg-person-subtle border-person/50 text-text', icon: UserRound, mark: 'text-person-ink' },
};

const inputCls = "w-full rounded-md bg-surface border border-border-strong px-3 py-2 text-sm text-text focus:border-accent focus:outline-none placeholder:text-text-faint";
const monoInputCls = `${inputCls} font-mono`;

// A row's quiet icon action: always there for a keyboard (never hover-only).
const rowAction = "inline-flex size-8 items-center justify-center rounded-md text-text-faint transition-colors hover:bg-surface-2 hover:text-text";

// ── Main component ─────────────────────────────────────────────────────────────

export const McpServersTab = ({
    mcpServers, loadingMcp, isConnecting,
    notice, onNotice,
    pendingServerName, onPendingResolved,
    draftMcpServer, setDraftMcpServer,
    onAddServer, onDeleteServer, onReconnectServer, children,
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
    const presets = serverType === 'stdio' ? STDIO_PRESETS : REMOTE_PRESETS;
    return (
        <AppScreen
            id="mcp_servers"
            width="wide"
            actions={
                <Button
                    variant="secondary"
                    onClick={() => refreshServers()}
                    disabled={isRefreshing}
                    title="Refresh server statuses"
                >
                    <RefreshCw className={`size-4 ${isRefreshing ? 'animate-spin' : ''}`} aria-hidden />
                    {isRefreshing ? 'Refreshing…' : 'Refresh'}
                </Button>
            }
        >
        <div className="space-y-8">
            <GoogleWorkspaceCard />

            {/* ── Inline notice ── */}
            {notice && (
                <div role="status" className={`flex items-start gap-2.5 px-4 py-3 rounded-lg border text-sm animate-in fade-in slide-in-from-top-2 duration-200 ${NOTICE_TONES[notice.tone].chip}`}>
                    {(() => { const Icon = NOTICE_TONES[notice.tone].icon; return <Icon className={`h-4 w-4 mt-0.5 shrink-0 ${NOTICE_TONES[notice.tone].mark}`} aria-hidden />; })()}
                    <span className="leading-relaxed">{notice.message}</span>
                </div>
            )}

            {/* ── Connected Servers ── */}
            <section className="space-y-3" aria-labelledby="mcp-connected-title">
                <h2 id="mcp-connected-title" className="text-section text-text">Connected Servers</h2>
                {loadingMcp ? (
                    <div className="flex items-center gap-2 text-text-faint text-sm">
                        <Spinner size={16} className="text-machine" /> Loading…
                    </div>
                ) : mcpServers.length === 0 ? (
                    <div className="rounded-xl border border-dashed border-border px-6 py-10 text-center">
                        <Server className="mx-auto mb-2 size-8 text-text-faint" aria-hidden />
                        <p className="text-sm font-medium text-text">No servers added yet.</p>
                        <p className="mt-1 text-sm text-text-muted">Pick a preset or fill the form below.</p>
                    </div>
                ) : (
                    <ul className="divide-y divide-border border-y border-border" data-mcp-list="">
                        {[...mcpServers]
                            .sort((a, b) => (a.label || a.name).localeCompare(b.label || b.name, undefined, { sensitivity: 'base' }))
                            .map((server) => {
                            const label = server.label || server.name;
                            // The route names a placeholder rather than serving the token itself (F135).
                            const tokenName: string | null = server.token_placeholder ?? null;
                            const placeholder = server.server_type === 'remote' && tokenName ? PLACEHOLDER_TOKENS[tokenName] : undefined;
                            return (
                            <li key={server.name} data-mcp-row={server.name} className="group flex items-center gap-3 px-2 py-3 transition-colors hover:bg-row-hover sm:gap-4">
                                <AppMark name={label} />
                                <div className="flex min-w-0 flex-1 flex-col gap-1">
                                    <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                                        <span className="text-sm font-semibold text-text">{label}</span>
                                        {server.label && server.label !== server.name && (
                                            <span className="text-xs text-text-faint font-mono">{server.name}</span>
                                        )}
                                        <TypePill type={server.server_type} />
                                    </div>
                                    <code className="truncate text-xs text-text-faint font-code">
                                        {server.server_type === 'remote'
                                            ? server.url
                                            : `${server.command} ${(server.args || []).join(' ')}`}
                                    </code>
                                    {placeholder && (
                                        <p className="text-xs text-text">
                                            Its token is <code className="font-code">{tokenName}</code>, a
                                            placeholder an older preset filled in, so it never signs in.{' '}
                                            {placeholder.needs === 'oauth'
                                                ? `Remove it and add it again from the ${placeholder.preset} preset to sign in with OAuth.`
                                                : `Remove it and add it again from the ${placeholder.preset} preset with a GitHub personal access token.`}
                                        </p>
                                    )}
                                </div>
                                <div className="hidden shrink-0 sm:block"><ServerStatus status={server.status} /></div>
                                <div className="flex shrink-0 items-center gap-0.5">
                                    <span className="sm:hidden"><ServerStatus status={server.status} /></span>
                                    {server.status === 'reauth_needed' && (
                                        <button onClick={() => onReconnectServer(server.name)} title="Re-authenticate" aria-label={`Re-authenticate ${label}`}
                                            className={`${rowAction} text-person-ink hover:text-person-ink`}>
                                            <ShieldAlert className="size-4" aria-hidden />
                                        </button>
                                    )}
                                    {(!server.status || server.status === 'disconnected') && (
                                        <button onClick={() => onReconnectServer(server.name)} title="Retry connection" aria-label={`Retry connection to ${label}`}
                                            className={rowAction}>
                                            <RefreshCw className="size-4" aria-hidden />
                                        </button>
                                    )}
                                    {server.status === 'connected' && (
                                        <button onClick={() => onReconnectServer(server.name)} title="Force reconnect (refresh stale session)" aria-label={`Force reconnect ${label}`}
                                            className={`${rowAction} sm:opacity-0 sm:group-hover:opacity-100 sm:focus-visible:opacity-100`}>
                                            <RefreshCw className="size-4" aria-hidden />
                                        </button>
                                    )}
                                    <button onClick={() => onDeleteServer(server.name)}
                                        aria-label={`Remove ${label}`}
                                        title={`Remove ${label}`}
                                        className={`${rowAction} hover:text-danger`}>
                                        <Trash className="size-4" aria-hidden />
                                    </button>
                                </div>
                            </li>
                            );
                        })}
                    </ul>
                )}
            </section>

            {/* ── Add Server Form ── */}
            <section className="space-y-6 rounded-xl border border-border bg-surface p-4 sm:p-6" aria-labelledby="mcp-add-title">
                <div className="flex flex-wrap items-center justify-between gap-3">
                    <h2 id="mcp-add-title" className="text-section text-text">Connect a server</h2>
                    {/* Type toggle */}
                    <div role="group" aria-label="Server type" className="inline-flex gap-0.5 rounded-lg bg-surface-2 p-0.5">
                        {([['stdio', 'Local (stdio)', Terminal], ['remote', 'Remote (URL)', Globe]] as const).map(([t, text, Icon]) => (
                            <button
                                key={t}
                                type="button"
                                aria-pressed={serverType === t}
                                onClick={() => setServerType(t)}
                                className={`inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 text-xs font-medium transition-colors ${serverType === t ? 'bg-bg text-text shadow-[0_0_0_1px_var(--border)]' : 'text-text-muted hover:text-text'}`}
                            >
                                <Icon className="size-3.5" aria-hidden /> {text}
                            </button>
                        ))}
                    </div>
                </div>

                {/* ── Presets ── */}
                <div className="space-y-2.5">
                    <h3 className="flex items-center gap-2 text-xs font-medium text-text-muted">
                        <Zap className="size-3.5 text-text-faint" aria-hidden />
                        {serverType === 'stdio' ? 'Local Presets' : 'Remote Presets'}
                    </h3>
                    <div className="flex flex-wrap gap-2">
                        {presets.map(p => (
                            <button key={p.name + p.label} type="button" onClick={() => applyPreset(p)}
                                className="inline-flex items-center gap-2 rounded-full border border-border bg-surface py-1 pl-1 pr-3 text-xs font-medium text-text-muted transition-colors hover:border-border-strong hover:text-text">
                                <AppMark name={p.label} size="sm" className="size-6 rounded-full text-[11px]" />
                                {p.label}
                            </button>
                        ))}
                    </div>
                    <p className="text-xs text-text-faint">
                        Find more on the{' '}
                        <a href="https://github.com/modelcontextprotocol/servers" target="_blank" rel="noopener noreferrer"
                            className="text-accent-ink underline underline-offset-2 hover:text-accent-hover transition-colors">
                            MCP servers registry
                        </a>.
                        {serverType === 'remote' && ' Remote servers use native OAuth — no npx required.'}
                    </p>
                </div>

                {/* ── Fields ── */}
                <div className="space-y-4">
                    {/* Display Label + Unique ID — always shown */}
                    <div className="grid gap-4 sm:grid-cols-2">
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
                            <div className="grid gap-4 sm:grid-cols-2">
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
                                    <Button variant="ghost" size="sm" onClick={addEnvVar}>
                                        <Plus className="size-3.5" aria-hidden /> Add variable
                                    </Button>
                                </div>
                                {draftMcpServer.env.map((env, i) => (
                                    <div key={i} className="flex gap-2">
                                        <input type="text" aria-label={`Variable ${i + 1} name`} placeholder="KEY" value={env.key}
                                            onChange={e => updateEnvVar(i, 'key', e.target.value)}
                                            className={`${monoInputCls} flex-1 text-xs`} />
                                        <input type="text" aria-label={`Variable ${i + 1} value`} placeholder="VALUE" value={env.value}
                                            onChange={e => updateEnvVar(i, 'value', e.target.value)}
                                            className={`${monoInputCls} flex-[2] text-xs`} />
                                        <button onClick={() => removeEnvVar(i)} aria-label={`Remove variable ${i + 1}`} className={`${rowAction} hover:text-danger`}>
                                            <Trash className="size-4" aria-hidden />
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
                                <p className="text-xs text-text-faint">
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
                                        aria-label={tokenVisible ? 'Hide token' : 'Show token'}
                                        className="absolute right-2 top-1/2 -translate-y-1/2 text-text-faint hover:text-text transition-colors"
                                    >
                                        {tokenVisible
                                            ? <EyeOff className="h-4 w-4" aria-hidden />
                                            : <Eye className="h-4 w-4" aria-hidden />}
                                    </button>
                                </div>
                                {appliedPreset && appliedPreset.url === draftMcpServer.url && appliedPreset.auth && (
                                    <p className="text-xs text-text-faint leading-relaxed">
                                        {appliedPreset.auth === 'oauth' && <>
                                            {appliedPreset.label} signs you in with OAuth: leave the token empty, and a browser
                                            window opens when you connect.
                                        </>}
                                        {appliedPreset.auth === 'bearer' && <>
                                            {appliedPreset.label} needs a token. {appliedPreset.tokenHint}{' '}
                                            {appliedPreset.tokenUrl && (
                                                <a href={appliedPreset.tokenUrl} target="_blank" rel="noopener noreferrer"
                                                    className="text-accent-ink underline underline-offset-2">Create one</a>
                                            )}
                                        </>}
                                        {appliedPreset.auth === 'none' && <>{appliedPreset.label} needs no account.</>}
                                    </p>
                                )}
                            </div>
                        </>
                    )}
                </div>

                {/* Submit: the page's one primary action */}
                <div className="flex justify-end">
                    <Button onClick={onAddServer} disabled={isConnecting} loading={isConnecting}>
                        {isConnecting ? 'Connecting…' : 'Connect Server'}
                    </Button>
                </div>
            </section>

            {children}
        </div>
        </AppScreen>
    );
};
