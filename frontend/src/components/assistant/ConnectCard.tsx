'use client';

/*
 * The AI builder's Connect card (BWA1, D274).
 *
 * When something needs a key, a token or a sign-in, the builder puts this card
 * up (`prepare_connection`) instead of asking for the secret in the chat. What
 * the person types here goes straight to the host's own routes through its
 * `Connector` (each product's panel has one) and is stored encrypted there; it
 * never enters the conversation, the builder's run or a model. When it is done
 * the card says "Connected …" in the conversation, so the builder carries on.
 *
 * Nothing connects silently: the card is the person's to press.
 */
import { AlertTriangle, ArrowRight, Check, Lock } from 'lucide-react';
import * as React from 'react';

import { Button, buttonStyles, Input } from '@/components/ui';
import { cn } from '@/lib/cn';
import type { ConnectSpec } from '@/lib/builderEvents';

/** What a host's adapter answers after an attempt. */
export type ConnectResult =
    | { status: 'connected'; message?: string }
    /** A sign-in to finish in another tab (OAuth); the card waits for "Done". */
    | { status: 'signin'; url: string }
    | { status: 'error'; message: string };

/**
 * A host's way to each kind of connection, through its own routes. A kind the
 * host leaves out is offered as a link to `href(screen)` instead.
 */
export interface Connector {
    app?: (app: NonNullable<ConnectSpec['app']>, token?: string) => Promise<ConnectResult>;
    server?: (server: NonNullable<ConnectSpec['server']>, token?: string) => Promise<ConnectResult>;
    providerKey?: (provider: NonNullable<ConnectSpec['provider']>, key: string) => Promise<ConnectResult>;
    toolSecret?: (tool: NonNullable<ConnectSpec['tool']>, value: string, hostParams?: Record<string, string>) => Promise<ConnectResult>;
    /** The host's path for a settings screen id (`models`, `apps`, `channels`…), or null if it has none. */
    href: (screen: string) => string | null;
    /** The product's name for "stored encrypted in …". */
    product?: string;
}

type Phase = 'idle' | 'working' | 'signin' | 'connected' | 'error';

/** The screen a kind falls back to when the host has no adapter for it. */
const FALLBACK_SCREEN: Record<ConnectSpec['kind'], string> = {
    app: 'apps', mcp_server: 'apps', provider_key: 'models', tool_secret: 'tools', settings: 'models',
};

function secretLabel(spec: ConnectSpec): string | null {
    switch (spec.kind) {
        case 'app':
            return spec.app?.auth === 'bearer' ? (spec.app?.token_hint || 'Access token') : null;
        case 'mcp_server':
            return 'Access token (leave empty to sign in instead)';
        case 'provider_key':
            return `${spec.provider?.label ?? spec.provider?.id ?? 'Provider'} API key`;
        case 'tool_secret':
            return spec.tool?.header ?? 'Key';
        default:
            return null;
    }
}

export function ConnectCard({
    spec, connector, onConnected,
}: {
    spec: ConnectSpec;
    connector: Connector;
    /** Says the result in the conversation ("Connected Notion."), which the builder reads. */
    onConnected: (text: string) => void;
}) {
    const [phase, setPhase] = React.useState<Phase>('idle');
    const [error, setError] = React.useState<string | null>(null);
    const [secret, setSecret] = React.useState('');
    // Each account part of a made tool's host (F945): filled in beside the key, checked by the server.
    const hostParams = spec.kind === 'tool_secret' ? (spec.tool?.host_params ?? []) : [];
    const [params, setParams] = React.useState<Record<string, string>>({});
    const paramsFilled = hostParams.every((p) => (params[p.name] ?? '').trim());
    const [signinUrl, setSigninUrl] = React.useState<string | null>(null);

    const name = spec.app?.name ?? spec.server?.label ?? spec.provider?.label ?? spec.tool?.name
        ?? spec.screen?.label ?? spec.title;
    const screen = spec.kind === 'settings' ? (spec.screen?.id ?? 'models') : FALLBACK_SCREEN[spec.kind];
    const run = (() => {
        if (spec.kind === 'app' && spec.app && connector.app) return (s: string) => connector.app!(spec.app!, s || undefined);
        if (spec.kind === 'mcp_server' && spec.server && connector.server) return (s: string) => connector.server!(spec.server!, s || undefined);
        if (spec.kind === 'provider_key' && spec.provider && connector.providerKey) return (s: string) => connector.providerKey!(spec.provider!, s);
        if (spec.kind === 'tool_secret' && spec.tool && connector.toolSecret) {
            return (s: string) => connector.toolSecret!(spec.tool!, s,
                Object.fromEntries(hostParams.map((p) => [p.name, (params[p.name] ?? '').trim()])));
        }
        return null;
    })();
    const link = run ? null : connector.href(screen);
    const field = run ? secretLabel(spec) : null;
    const needsSecret = spec.kind === 'provider_key' || spec.kind === 'tool_secret';
    const done = phase === 'connected';

    async function connect() {
        if (!run || !paramsFilled) return;
        setPhase('working');
        setError(null);
        try {
            const result = await run(secret.trim());
            if (result.status === 'connected') {
                setSecret('');
                setPhase('connected');
                onConnected(result.message ?? `Connected ${name}.`);
            } else if (result.status === 'signin') {
                setSigninUrl(result.url);
                setPhase('signin');
                window.open(result.url, '_blank', 'noopener');
            } else {
                setError(result.message);
                setPhase('error');
            }
        } catch (exc) {
            setError(exc instanceof Error ? exc.message : 'That did not work. Try again.');
            setPhase('error');
        }
    }

    /** Set up on its own screen (F1104): the press only says so; the build checks what is here, so it claims nothing. */
    function setUp() {
        onConnected(`Connected ${spec.waits_for}.`);
    }

    function signedIn() {
        setSecret('');
        setPhase('connected');
        onConnected(`Signed in to ${name}.`);
    }

    return (
        <section
            aria-label={spec.title}
            className={cn(
                'flex min-w-0 flex-col gap-3 rounded-xl border bg-surface px-4 py-3.5',
                done ? 'border-border' : 'border-person/60',
            )}
        >
            <div className="flex items-center gap-2 text-xs">
                {done ? (
                    <span className="inline-flex items-center gap-1.5 font-medium text-success">
                        <Check className="size-3.5" aria-hidden />
                        Connected
                    </span>
                ) : (
                    <span className="inline-flex items-center gap-1.5 font-medium text-person-ink">
                        <span aria-hidden className="size-2 rounded-full bg-person" />
                        Needs you
                    </span>
                )}
                {!done && <span className="text-text-faint">· Connect</span>}
            </div>

            <div className="flex flex-col gap-1">
                <span className="text-sm font-medium text-text">{spec.title}</span>
                {spec.why && <span className="text-sm leading-relaxed text-text-muted">{spec.why}</span>}
                {spec.server?.url && (
                    <span className="break-all font-mono text-xs text-text-muted">{spec.server.url}</span>
                )}
            </div>

            {/* Where the key goes, before any field it is typed in (F1244). */}
            {!done && spec.kind === 'tool_secret' && (spec.tool?.sends_to?.length ?? 0) > 0 && (
                <div className="flex flex-col gap-1.5 text-sm">
                    <span className="text-text-muted">
                        Keys will be sent to{' '}
                        <span className="break-all font-mono text-text">{spec.tool!.sends_to!.join(', ')}</span>
                    </span>
                    {(spec.tool!.unsure ?? []).map((host) => (
                        <span key={host} className="inline-flex items-start gap-1.5 text-warning">
                            <AlertTriangle className="mt-0.5 size-3.5 shrink-0" aria-hidden />
                            <span>
                                <span className="font-mono">{host}</span> doesn&apos;t look like {name}&apos;s own
                                domain. Check it&apos;s theirs before you enter a key.
                            </span>
                        </span>
                    ))}
                </div>
            )}

            {!done && run && hostParams.map((p) => (
                <label key={p.name} className="flex flex-col gap-1.5">
                    <span className="text-xs font-medium text-text-muted">{p.says || p.name}</span>
                    <Input
                        autoComplete="off"
                        spellCheck={false}
                        value={params[p.name] ?? ''}
                        onChange={(e) => setParams((was) => ({ ...was, [p.name]: e.target.value }))}
                        aria-label={p.says || p.name}
                    />
                </label>
            ))}

            {!done && field && phase !== 'signin' && (
                <label className="flex flex-col gap-1.5">
                    <span className="text-xs font-medium text-text-muted">{field}</span>
                    <Input
                        type="password"
                        autoComplete="off"
                        spellCheck={false}
                        value={secret}
                        onChange={(e) => setSecret(e.target.value)}
                        onKeyDown={(e) => { if (e.key === 'Enter' && (secret || !needsSecret)) void connect(); }}
                        aria-label={field}
                    />
                </label>
            )}

            {phase === 'signin' && (
                <p className="text-sm text-text-muted">
                    Finish signing in on the tab that opened, then come back and press Done.
                    {signinUrl && (
                        <> <a href={signinUrl} target="_blank" rel="noopener noreferrer" className="text-accent-ink underline">Open it again</a>.</>
                    )}
                </p>
            )}

            {error && <p role="alert" className="text-sm text-danger">{error}</p>}

            {!done && (
                <div className="flex flex-wrap items-center justify-between gap-2 border-t border-border pt-2.5">
                    <span className="inline-flex items-center gap-1.5 text-xs text-text-faint">
                        <Lock className="size-3" aria-hidden />
                        {run && needsSecret
                            ? `Paste it here, never in the chat: it goes straight to ${connector.product ?? 'your workspace'}, stored encrypted.`
                            : run
                            ? `Goes straight to ${connector.product ?? 'your workspace'}, stored encrypted. Never into this chat.`
                            : 'You set it up on that screen. Nothing goes into this chat.'}
                    </span>
                    {phase === 'signin' ? (
                        <Button type="button" size="sm" onClick={signedIn}>Done</Button>
                    ) : run ? (
                        <Button
                            type="button"
                            size="sm"
                            loading={phase === 'working'}
                            onClick={() => void connect()}
                            disabled={phase === 'working' || (needsSecret && !secret.trim()) || !paramsFilled}
                        >
                            {needsSecret ? 'Save' : 'Connect'}
                        </Button>
                    ) : link ? (
                        <span className="inline-flex flex-wrap items-center gap-2">
                            <a href={link} className={buttonStyles({ variant: 'secondary', size: 'sm' })}>
                                Open {spec.screen?.label ?? 'settings'}
                                <ArrowRight className="size-3.5" aria-hidden />
                            </a>
                            {spec.waits_for && (
                                <Button type="button" size="sm" onClick={setUp}>Connected</Button>
                            )}
                        </span>
                    ) : null}
                </div>
            )}
        </section>
    );
}
