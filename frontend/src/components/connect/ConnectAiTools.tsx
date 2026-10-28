"use client";

/**
 * Connect to AI tools: use Synapse from Claude, Codex, Cursor or VS Code (GUIDE1, D318, F915).
 *
 * Naveen: "under settings its hard to find connect our mcp to third party agents ... we should have a separate mcp
 * section menu that says connect to claude, codex, cursor with mcp configuration setting ... also a small content
 * on what they can do after connecting". A path, as he likes a flow to read: pick the tool, make a key for it, paste
 * what it needs, see it connect, then what to ask. The same screen in both products (synced like
 * `components/run`); each product's `ConnectHost` says where its MCP door is and how keys are made and read.
 *
 * "Connected" is the key's own `last_used_at` moving (both products stamp it on every authenticated call, cloud at
 * most once a minute after the first, F165), so the check needs no endpoint of its own.
 */

import { Check } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';

import { AppMark, Button, Checkbox, CopyButton, ErrorNote, Field, Input, TextLink } from '@/components/ui';
import { cn } from '@/lib/cn';

import {
    AI_CLIENTS, WHAT_TO_ASK, canSignIn, needsKey, signInSnippet, signInWhere, snippetFor, type AiClientId, type DesktopRoute,
} from './clients';

export type ConnectHost = {
    /** The product's MCP endpoint, absolute. */
    mcpUrl: string;
    /** How Claude Desktop connects here; `unavailable` leaves it off the list. */
    desktop: DesktopRoute;
    /** This product's logo paths. */
    logos: { claude: string; openai: string; cursor: string; vscode: string };
    /**
     * Tools that can sign in do, instead of a key (F1178): this product has an OAuth server and its connectors are on
     * (cloud). OSS has none, so it never sets this and every tool keeps the key.
     */
    signIn?: boolean;
    /** Where a person sees and removes what they connected by signing in (cloud: Settings › Developer). */
    connectedAppsHref?: string;
    /** Makes a key named for the tool. Null when this person can't make keys here (cloud: owners and admins). */
    makeKey: ((name: string, canBuild: boolean) => Promise<{ id: string; key: string }>) | null;
    /** Keys here only run things unless made to build too (cloud, F847): offer that choice, with its trade-off. */
    buildChoice?: boolean;
    /** When a key last authenticated, or null if never. */
    lastUsed: (id: string) => Promise<string | null>;
    /** This workspace's keys, for Home's card to replace its own unused ones (F1027). */
    listKeys?: () => Promise<{ id: string; name: string; last_used_at: string | null }[]>;
    revokeKey?: (id: string) => Promise<void>;
    /** Where the keys are listed and revoked. */
    keysHref?: string;
    /** Who to ask for a key, when `makeKey` is null. */
    askForKey?: string;
    /** The docs page for the MCP server. */
    guideHref: string;
};

/** How often the check asks for the key's last use, and for how long. */
const POLL_MS = 4000;
const POLL_FOR_MS = 15 * 60_000;

export function ConnectAiTools({ host }: { host: ConnectHost }) {
    const clients = useMemo(
        () => AI_CLIENTS.filter(c => c.id !== 'claude_desktop' || host.desktop !== 'unavailable'),
        [host.desktop],
    );
    const [clientId, setClientId] = useState<AiClientId>('claude_code');
    const client = clients.find(c => c.id === clientId) ?? clients[0];
    // Sign in where this product and the tool both can; the key is one click away, and the only way in OSS.
    const [useKey, setUseKey] = useState(false);
    const signsIn = Boolean(host.signIn) && canSignIn(client.id) && !useKey;
    const wantsKey = needsKey(client.id, host.desktop) && !signsIn;

    const [made, setMade] = useState<{ id: string; key: string; name: string } | null>(null);
    const [pasted, setPasted] = useState('');
    const [making, setMaking] = useState(false);
    const [canBuild, setCanBuild] = useState(false);
    const [problem, setProblem] = useState('');
    const key = made?.key ?? pasted.trim();
    const snippet = signsIn
        ? signInSnippet(client.id, host.mcpUrl)
        : snippetFor(client.id, { url: host.mcpUrl, key, desktop: host.desktop });

    async function makeKey() {
        if (!host.makeKey) return;
        setProblem('');
        setMaking(true);
        try {
            // Named for the tool it was made for; it works for any of them, so switching keeps it.
            setMade({ ...(await host.makeKey(client.label, canBuild)), name: client.label });
        } catch (err) {
            setProblem(err instanceof Error ? err.message : 'The key could not be made.');
        } finally {
            setMaking(false);
        }
    }

    const connectedAt = useFirstUse(made?.id ?? null, host.lastUsed);
    const stepOffset = wantsKey ? 0 : -1;

    return (
        <div className="max-w-3xl space-y-8" data-tour="connect-ai-tools">
            <Step n={1} title="Pick your AI tool">
                <div className="flex flex-wrap gap-2" role="radiogroup" aria-label="AI tool">
                    {clients.map(c => {
                        const on = c.id === client.id;
                        return (
                            <button
                                key={c.id}
                                type="button"
                                role="radio"
                                aria-checked={on}
                                onClick={() => setClientId(c.id)}
                                className={cn(
                                    'inline-flex items-center gap-2.5 rounded-md border px-3 py-2 text-sm font-medium transition-colors',
                                    on
                                        ? 'border-accent/40 bg-accent-subtle text-accent-ink'
                                        : 'border-border text-text-muted hover:border-border-strong hover:text-text',
                                )}
                            >
                                <AppMark name={c.label} imgSrc={c.logo ? host.logos[c.logo] : undefined} size="sm" />
                                {c.label}
                            </button>
                        );
                    })}
                </div>
            </Step>

            {wantsKey && (
                <Step n={2} title={`Make a key for ${client.label}`}>
                    <p className="text-sm text-text-muted">
                        A key lets {client.label} act in this workspace. Make one just for it, so you can remove it
                        on its own later in API keys.
                    </p>
                    {made ? (
                        <p className="flex items-center gap-2 text-sm text-text">
                            <Check className="size-4 text-success" aria-hidden />
                            Key “{made.name}” made. It is filled in below and shown only on this page: copy the
                            setup before you leave.
                        </p>
                    ) : host.makeKey ? (
                        <div className="space-y-3">
                            {host.buildChoice && (
                                <label className="flex items-start gap-2.5 text-sm">
                                    <Checkbox checked={canBuild} onChange={setCanBuild} label="Can also build" className="mt-0.5" />
                                    <span>
                                        <span className="font-medium text-text">Can also build</span>
                                        <span className="block text-xs text-text-faint">
                                            On: {client.label} can also create and change agents and workflows. Off: it
                                            runs them, answers their questions and reads results, and changes nothing.
                                        </span>
                                    </span>
                                </label>
                            )}
                            <Button onClick={() => void makeKey()} loading={making}>Make a key for {client.label}</Button>
                        </div>
                    ) : (
                        <p className="text-sm text-text-muted">{host.askForKey ?? 'Ask an owner or admin of this workspace for a key.'}</p>
                    )}
                    {!made && (
                        <Field label="Or paste a key you already have" hint="Only used to fill in the setup below; it isn't saved.">
                            <Input
                                type="password"
                                autoComplete="off"
                                value={pasted}
                                onChange={e => setPasted(e.target.value)}
                                placeholder="sk-syn-…"
                                aria-label="A key you already have"
                            />
                        </Field>
                    )}
                    <ErrorNote>{problem}</ErrorNote>
                </Step>
            )}

            <Step n={3 + stepOffset} title={`Add Synapse to ${client.label}`}>
                {client.id === 'claude_desktop' && host.desktop === 'oauth' ? (
                    <>
                        <p className="text-sm text-text-muted">
                            In Claude Desktop or claude.ai: Settings → Connectors → Add custom connector, and paste
                            this address. Claude asks you to sign in and pick this workspace.
                        </p>
                        <Snippet text={host.mcpUrl} />
                    </>
                ) : snippet ? (
                    <>
                        <p className="text-sm text-text-muted">{signsIn ? signInWhere(client.id) : client.where}</p>
                        <Snippet text={snippet} />
                        {signsIn && (
                            <p className="text-xs text-text-faint">
                                Signing in connects {client.label} as you: it can do what your role can here, up to
                                building, while members, billing, keys and settings stay in this app, and what it does
                                is recorded as yours.
                                {host.connectedAppsHref && (
                                    <> Remove it any time in <TextLink href={host.connectedAppsHref}>Connected apps</TextLink>.</>
                                )}{' '}
                                <button type="button" onClick={() => setUseKey(true)} className="font-medium text-accent-ink hover:underline">
                                    Use a key instead
                                </button>
                            </p>
                        )}
                        {useKey && host.signIn && canSignIn(client.id) && (
                            <button type="button" onClick={() => setUseKey(false)} className="text-xs font-medium text-accent-ink hover:underline">
                                Sign in instead of a key
                            </button>
                        )}
                        {client.id === 'claude_desktop' && (
                            <p className="text-xs text-text-faint">
                                This starts <code className="font-code">synapse mcp</code> on this computer, which
                                uses the Synapse CLI&apos;s own sign-in. If Claude can&apos;t find it, put the full
                                path from <code className="font-code">which synapse</code> in place of “synapse”.
                            </p>
                        )}
                    </>
                ) : null}
            </Step>

            <Step n={4 + stepOffset} title="Check it works">
                {/* hydration-safe: `connectedAt` is only ever set after mount, by the poll, so no server render formats it. */}
                {connectedAt ? (
                    <p className="flex items-center gap-2 text-sm font-medium text-text" role="status">
                        <Check className="size-4 text-success" aria-hidden />
                        Connected: {client.label} reached Synapse at {new Date(connectedAt).toLocaleTimeString()}.
                    </p>
                ) : (
                    <p className="text-sm text-text-muted" role="status">
                        Ask {client.label}: <span className="text-text">“List my Synapse workflows.”</span>{' '}
                        {made ? 'This page shows it as soon as the first call arrives.' : 'If it answers with your workflows, it is connected.'}
                    </p>
                )}
            </Step>

            <Step n={5 + stepOffset} title="What you can ask it">
                <ul className="space-y-2.5">
                    {WHAT_TO_ASK.map(w => (
                        <li key={w.ask} className="text-sm">
                            <span className="text-text">“{w.ask}”</span>
                            <span className="block text-xs text-text-faint">{w.does}</span>
                        </li>
                    ))}
                </ul>
                <p className="text-xs text-text-faint">
                    Deleting stays in this app. More in{' '}
                    <TextLink external href={host.guideHref} target="_blank" rel="noopener noreferrer">the guide to connecting AI tools</TextLink>.
                </p>
            </Step>
        </div>
    );
}

function Step({ n, title, children }: { n: number; title: string; children: React.ReactNode }) {
    return (
        <section className="flex gap-4">
            <span
                aria-hidden
                className="flex size-7 shrink-0 items-center justify-center rounded-full border border-border-strong text-xs font-semibold tabular-nums text-text-muted"
            >
                {n}
            </span>
            <div className="min-w-0 flex-1 space-y-3 pt-0.5">
                <h2 className="text-sm font-semibold text-text">{title}</h2>
                {children}
            </div>
        </section>
    );
}

function Snippet({ text }: { text: string }) {
    return (
        // Copy sits in its own bar, not over the code: at a phone's width a long command scrolled under it.
        <div className="rounded-md border border-border bg-surface-2">
            <div className="flex justify-end border-b border-border px-2 py-1.5">
                <CopyButton text={text} />
            </div>
            <pre className="overflow-x-auto px-4 py-3 font-code text-xs leading-relaxed text-text">{text}</pre>
        </div>
    );
}

/** The key's first use after it was made: asked every few seconds, for a while, until it comes. */
function useFirstUse(id: string | null, lastUsed: ConnectHost['lastUsed']): string | null {
    // Kept with the key it was read for, so a new key starts unconnected without a reset.
    const [seen, setSeen] = useState<{ id: string; at: string } | null>(null);
    useEffect(() => {
        if (!id) return;
        const started = Date.now();
        let stopped = false;
        const timer = setInterval(async () => {
            if (stopped) return;
            if (Date.now() - started > POLL_FOR_MS) {
                clearInterval(timer);
                return;
            }
            try {
                const when = await lastUsed(id);
                if (when && !stopped) {
                    setSeen({ id, at: when });
                    clearInterval(timer);
                }
            } catch {
                // A missed read is asked again on the next tick.
            }
        }, POLL_MS);
        return () => {
            stopped = true;
            clearInterval(timer);
        };
    }, [id, lastUsed]);
    return seen && seen.id === id ? seen.at : null;
}
