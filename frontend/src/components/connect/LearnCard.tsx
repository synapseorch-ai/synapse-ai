"use client";

/**
 * Home's "Learn Synapse with your AI assistant" (GUIDE1, D318, F1030). Naveen: "since docs MCP will be open... let's
 * have a button or link for new users to take them to the claude or codex to make them understand the product more
 * with docs MCP". The docs' MCP server is public, so nothing here needs a key: Cursor and VS Code add it from their
 * install link, Claude Code and Codex from one copied command, Claude from its connectors page with the address
 * copied. A first question to ask is offered to copy.
 *
 * Shown to a new workspace (beside Getting started) until put away, and only once the docs' server answers
 * (`docsMcpAnswers`, on the server). Synced with the rest of `components/connect`.
 */

import { Check, X } from 'lucide-react';
import { useState } from 'react';

import { AppMark, Button, CopyButton, IconButton, TextLink } from '@/components/ui';

import { CLAUDE_CONNECTORS, DOCS_MCP_URL, installLink, publicCommand, type AiClientId } from './clients';

const NAME = 'synapse-docs';

/** A first question that shows what Synapse is for, answered from its docs. */
export const FIRST_QUESTION = 'Using the Synapse docs, how do I build a workflow that checks new supplier invoices every Monday and asks me before paying any over $1,000?';

export function LearnCard({ logos, onDismiss }: { logos: { claude: string; openai: string }; onDismiss: () => void }) {
    const [done, setDone] = useState<{ said: string; text?: string } | null>(null);

    async function add(client: AiClientId | 'claude', label: string) {
        if (client === 'claude') {
            window.open(CLAUDE_CONNECTORS, '_blank', 'noopener,noreferrer');
            await copy(DOCS_MCP_URL);
            setDone({ said: 'In Claude, choose “Add custom connector” and paste this address. No sign-in needed.', text: DOCS_MCP_URL });
            return;
        }
        const link = installLink(client, { url: DOCS_MCP_URL, name: NAME });
        if (link) {
            window.location.assign(link);
            setDone({ said: `${label} should open and ask to add Synapse’s docs. Say yes, then ask it the question below.` });
            return;
        }
        const command = publicCommand(client, DOCS_MCP_URL, NAME) ?? '';
        await copy(command);
        setDone({ said: `Paste this in a terminal, then ask ${label} the question below.`, text: command });
    }

    const tools: { id: AiClientId | 'claude'; label: string; logo?: 'claude' | 'openai' }[] = [
        { id: 'claude', label: 'Claude', logo: 'claude' },
        { id: 'claude_code', label: 'Claude Code', logo: 'claude' },
        { id: 'codex', label: 'Codex', logo: 'openai' },
        { id: 'cursor', label: 'Cursor' },
        { id: 'vscode', label: 'VS Code' },
    ];

    return (
        <section data-tour="home-learn" aria-labelledby="home-learn-title" className="rounded-lg border border-border bg-surface px-5 py-4">
            <div className="flex items-start justify-between gap-3">
                <div>
                    <h2 id="home-learn-title" className="text-sm font-semibold text-text">Learn Synapse with your AI assistant</h2>
                    <p className="mt-0.5 text-sm text-text-muted">
                        Give Claude, Codex, Cursor or VS Code the Synapse docs, then ask it how to build what you need.
                    </p>
                </div>
                <IconButton label="Hide “Learn Synapse with your AI assistant”" icon={X} size="sm" onClick={onDismiss} />
            </div>
            <div className="mt-3 flex flex-wrap gap-2">
                {tools.map(t => (
                    <Button key={t.id} variant="secondary" onClick={() => void add(t.id, t.label)}>
                        <AppMark name={t.label} imgSrc={t.logo ? logos[t.logo] : undefined} size="sm" className="-my-1 -ml-1" />
                        Add to {t.label}
                    </Button>
                ))}
            </div>
            {done && (
                <div className="mt-3 space-y-2" role="status">
                    <p className="flex items-start gap-2 text-sm text-text">
                        <Check className="mt-0.5 size-4 shrink-0 text-success" aria-hidden />
                        <span className="min-w-0">{done.said}</span>
                    </p>
                    {done.text && <Line text={done.text} />}
                </div>
            )}
            <div className="mt-3 space-y-1.5">
                <p className="text-xs font-medium text-text-muted">A first question to ask</p>
                <Line text={FIRST_QUESTION} wrap />
                <p className="text-xs text-text-faint">
                    Or read them yourself: <TextLink external href={DOCS_MCP_URL.replace(/\/api\/mcp$/, '')} target="_blank" rel="noopener noreferrer">the Synapse docs</TextLink>.
                </p>
            </div>
        </section>
    );
}

async function copy(text: string): Promise<void> {
    try {
        await navigator.clipboard.writeText(text);
    } catch {
        // No clipboard: the text is on the card with its own Copy.
    }
}

function Line({ text, wrap }: { text: string; wrap?: boolean }) {
    return (
        <div className="flex items-start gap-2">
            <code className={`min-w-0 flex-1 rounded-md border border-border bg-surface-2 px-3 py-2 text-xs text-text ${wrap ? 'whitespace-normal font-sans' : 'overflow-x-auto whitespace-pre font-code'}`}>
                {text}
            </code>
            <CopyButton text={text} />
        </div>
    );
}
