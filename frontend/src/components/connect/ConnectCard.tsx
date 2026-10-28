"use client";

/**
 * Home's "Use Synapse from your AI tools" (GUIDE1, D318, F1027). Naveen, seeing the AI tools page: "I don't want new
 * ai tools menu... Let's have a home page card... instead of instructions can we have like a button that will just
 * get the things done of connecting... If instructions are what we get them let's have a button that takes to an MCP
 * server page in settings". So: one button per tool, doing as much as that tool lets a page do —
 *
 *   · Cursor and VS Code: make a key, open the tool's install link; nothing to paste.
 *   · Claude Code and Codex: make a key, copy the one command; paste it in a terminal.
 *   · Claude (cloud, once OAuth is on): copy the address, open Claude's connectors; it signs in, no key.
 *
 * Where the product signs in (cloud, F1178), every tool that can does: the link or command holds only the address and
 * the tool signs in, so no key is made, and anyone who may connect sees the buttons, not only those who make keys.
 *
 * "More options" is Settings › MCP server: every tool step by step (`ConnectAiTools`). Someone who can't make keys
 * here (cloud: not an owner or admin) is sent there for who to ask. Synced with the rest of `components/connect`.
 */

import { Check, X } from 'lucide-react';
import { useState } from 'react';

import { AppMark, Button, CopyButton, IconButton, TextLink } from '@/components/ui';

import {
    CLAUDE_CONNECTORS, canSignIn, homeKeyName, installLink, signInSnippet, signInWhere, snippetFor, unusedHomeKeys,
    type AiClient, type AiClientId,
} from './clients';
import type { ConnectHost } from './ConnectAiTools';

/** What happened, and the fallback when a browser refuses the clipboard or the app link after the key's round trip. */
type Done = { said: string; link?: { href: string; label: string }; text?: string; keyMade?: boolean };

export function ConnectCard({
    host, settingsHref, onDismiss,
}: {
    host: ConnectHost;
    /** Settings › MCP server, this product's address for it. */
    settingsHref: string;
    onDismiss: () => void;
}) {
    const [busy, setBusy] = useState<string | null>(null);
    const [done, setDone] = useState<Done | null>(null);
    const [problem, setProblem] = useState('');

    async function withKey(client: AiClientId, label: string) {
        if (!host.makeKey) return;
        setProblem('');
        setBusy(client);
        try {
            // A repeat click replaces this client's unused Home key instead of leaving one per click.
            if (host.listKeys && host.revokeKey) {
                for (const id of unusedHomeKeys(await host.listKeys(), label)) await host.revokeKey(id);
            }
            // Least privilege: the key rides in the link, so it only runs things (cloud's run-only, F847).
            const { key } = await host.makeKey(homeKeyName(label), false);
            const link = installLink(client, { url: host.mcpUrl, key });
            if (link) {
                window.location.assign(link);
                setDone({
                    said: `${label} should open and ask to add Synapse. Say yes, and it's connected. A key named “${homeKeyName(label)}” was made for it.`,
                    link: { href: link, label: `Open ${label}` },
                    keyMade: true,
                });
            } else {
                const command = snippetFor(client, { url: host.mcpUrl, key, desktop: host.desktop }) ?? '';
                let copied = true;
                try {
                    await navigator.clipboard.writeText(command);
                } catch {
                    copied = false;
                }
                setDone({
                    said: copied
                        ? `Copied. Paste it in a terminal and ${label} is connected. A key named “${homeKeyName(label)}” was made for it, shown only here.`
                        : `Paste this in a terminal and ${label} is connected. A key named “${homeKeyName(label)}” was made for it, shown only here.`,
                    text: command,
                    keyMade: true,
                });
            }
        } catch (err) {
            setProblem(err instanceof Error ? err.message : 'That did not work. Try More options.');
        } finally {
            setBusy(null);
        }
    }

    /** A tool that signs in: its address alone, in its link or its command; no key is made (F1178). */
    async function signIn(client: AiClientId, label: string) {
        setProblem('');
        const link = installLink(client, { url: host.mcpUrl });
        if (link) {
            window.location.assign(link);
            setDone({ said: `${label} should open and ask to add Synapse. Say yes, then sign in when it asks.`, link: { href: link, label: `Open ${label}` } });
            return;
        }
        const command = signInSnippet(client, host.mcpUrl) ?? '';
        let copied = true;
        try {
            await navigator.clipboard.writeText(command);
        } catch {
            copied = false;
        }
        setDone({ said: `${copied ? 'Copied. ' : ''}${signInWhere(client)}`, text: command });
    }

    async function claude() {
        setProblem('');
        // Opened first, while the click still counts as the person's: a pop-up after an await can be blocked.
        window.open(CLAUDE_CONNECTORS, '_blank', 'noopener,noreferrer');
        try {
            await navigator.clipboard.writeText(host.mcpUrl);
        } catch {
            // No clipboard: the address is shown below with its own Copy.
        }
        setDone({
            said: 'In Claude, choose “Add custom connector”, paste this address, and sign in.',
            text: host.mcpUrl,
            link: { href: CLAUDE_CONNECTORS, label: 'Open Claude’s connectors' },
        });
    }

    const keyed: { id: AiClientId; label: string; logo?: AiClient['logo'] }[] = [
        { id: 'cursor', label: 'Cursor', logo: 'cursor' },
        { id: 'vscode', label: 'VS Code', logo: 'vscode' },
        { id: 'claude_code', label: 'Claude Code', logo: 'claude' },
        { id: 'codex', label: 'Codex', logo: 'openai' },
    ];
    const signsIn = (client: AiClientId) => Boolean(host.signIn) && canSignIn(client);
    const mark = (label: string, logo?: AiClient['logo']) => (
        <AppMark name={label} imgSrc={logo ? host.logos[logo] : undefined} size="sm" className="-my-1 -ml-1" />
    );

    return (
        <section data-tour="home-connect" aria-labelledby="home-connect-title" className="rounded-lg border border-border bg-surface px-5 py-4">
            <div className="flex items-start justify-between gap-3">
                <div>
                    <h2 id="home-connect-title" className="text-sm font-semibold text-text">Use Synapse from your AI tools</h2>
                    <p className="mt-0.5 text-sm text-text-muted">
                        Run your workflows, answer approvals and check results from the AI tool you already work in.
                    </p>
                </div>
                <IconButton label="Hide “Use Synapse from your AI tools”" icon={X} size="sm" onClick={onDismiss} />
            </div>
            <div className="mt-3 flex flex-wrap items-center gap-2">
                {host.desktop === 'oauth' && (
                    <Button variant="secondary" onClick={() => void claude()}>{mark('Claude', 'claude')} Add to Claude</Button>
                )}
                {keyed.map(c => (
                    signsIn(c.id) || host.makeKey ? (
                        <Button
                            key={c.id}
                            variant="secondary"
                            loading={busy === c.id}
                            onClick={() => void (signsIn(c.id) ? signIn(c.id, c.label) : withKey(c.id, c.label))}
                        >
                            {mark(c.label, c.logo)} {installLink(c.id, { url: host.mcpUrl }) ? `Add to ${c.label}` : c.label}
                        </Button>
                    ) : null
                ))}
                <TextLink href={settingsHref} className="ml-1 text-sm">
                    {host.makeKey ? 'More options' : 'How to connect'}
                </TextLink>
            </div>
            {done && (
                <div className="mt-3 space-y-2" role="status">
                    <p className="flex items-start gap-2 text-sm text-text">
                        <Check className="mt-0.5 size-4 shrink-0 text-success" aria-hidden />
                        <span className="min-w-0">{done.said}</span>
                    </p>
                    {done.text && (
                        <div className="flex items-start gap-2">
                            <code className="min-w-0 flex-1 overflow-x-auto whitespace-pre rounded-md border border-border bg-surface-2 px-3 py-2 font-code text-xs text-text">
                                {done.text}
                            </code>
                            <CopyButton text={done.text} />
                        </div>
                    )}
                    {done.keyMade && host.keysHref && (
                        <p className="text-xs text-text-faint">
                            Remove it any time in <TextLink href={host.keysHref}>your keys</TextLink>.
                        </p>
                    )}
                    {done.link && (
                        done.link.href.startsWith('https://') ? (
                            <TextLink external href={done.link.href} target="_blank" rel="noopener noreferrer" className="text-sm">
                                {done.link.label}
                            </TextLink>
                        ) : (
                            // An app's own link (cursor://, vscode:) opens the app, not a tab.
                            <TextLink href={done.link.href} className="text-sm">{done.link.label}</TextLink>
                        )
                    )}
                </div>
            )}
            {problem && <p className="mt-3 text-sm text-danger" role="alert">{problem}</p>}
        </section>
    );
}
