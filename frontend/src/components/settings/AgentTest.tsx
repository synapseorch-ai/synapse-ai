'use client';

/*
 * The agents workbench's test chat, open source's half (M2d, D211).
 *
 * The panel and its turns are the shared `AgentTestPanel` / `testChat.ts`; this
 * is the transport. A test is an ordinary chat turn with the saved agent:
 * `POST /api/chat/stream` with the agent's id and a conversation of its own, so
 * nothing the panel does changes which agent chat has open (it never calls
 * `/api/agents/active`). The conversation is kept like any other and shows in
 * chat's history; Clear starts a new one, and so does opening another agent.
 */
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { useCallback, useEffect, useRef, useState } from 'react';

import { AgentTestPanel, agentContext, endTest, newTurn, reduceTest, type TestEvent, type TestTurn } from '@/components/assistant';
import { AttachButton, AttachmentChips, AttachmentError, useImageAttachments } from '@/components/app/ImageAttachments';
import { readWithStallTimeout } from '@/lib/sse';
import { humanizeToolName } from '@/lib/toolNames';

/* eslint-disable @typescript-eslint/no-explicit-any */

function newSessionId(): string {
    const c = (globalThis as any).crypto;
    return c?.randomUUID?.() ?? `sess_${Date.now()}_${Math.random().toString(16).slice(2)}`;
}

/** What `/api/status` says about one agent: whether attaching an image does anything. */
export type AgentStatus = { can_see_images?: boolean; attach_problem?: string } | undefined;

export function AgentTest({
    agent, dirty, onSave, saving, status, toolNames, knowledgeNames, onCollapse, className,
}: {
    /** The agent as saved; `null` while the open draft has never been saved. */
    agent: any | null;
    dirty: boolean;
    onSave: () => void;
    saving: boolean;
    status: AgentStatus;
    /** The saved agent's tools as a person reads them, or 'all'. */
    toolNames: string[] | 'all';
    knowledgeNames: string[];
    onCollapse?: () => void;
    className?: string;
}) {
    const agentId: string | null = agent?.id ?? null;
    const router = useRouter();
    const [turns, setTurns] = useState<TestTurn[]>([]);
    const [input, setInput] = useState('');
    const [busy, setBusy] = useState(false);
    const session = useRef<string>(newSessionId());
    const abort = useRef<AbortController | null>(null);

    const refusal = status && status.can_see_images === false
        ? status.attach_problem || "This agent's model cannot read images"
        : '';
    const attachments = useImageAttachments(refusal);

    const update = useCallback((id: string, f: (t: TestTurn) => TestTurn) => {
        setTurns(prev => prev.map(t => (t.id === id ? f(t) : t)));
    }, []);

    const clear = useCallback(() => {
        abort.current?.abort('clear');
        abort.current = null;
        session.current = newSessionId();
        setTurns([]);
        setBusy(false);
    }, []);

    // A fresh conversation for each agent opened (Naveen, M2d).
    useEffect(() => { clear(); }, [agentId, clear]);
    useEffect(() => () => abort.current?.abort('leave'), []);

    const send = async () => {
        if (!agentId || busy) return;
        const question = input.trim();
        const images = attachments.images.map(i => i.base64);
        if (!question && images.length === 0) return;
        const id = `t${Date.now()}`;
        setTurns(prev => [...prev, newTurn(id, question, images.length)]);
        setInput('');
        attachments.clear();
        setBusy(true);
        const controller = new AbortController();
        abort.current = controller;
        try {
            const res = await fetch('/api/chat/stream', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ message: question, session_id: session.current, agent_id: agentId, images }),
                signal: controller.signal,
            });
            if (res.status === 409) {
                update(id, t => endTest(t, 'A reply is still being written in this test. Wait for it, or press Clear.'));
                return;
            }
            if (!res.ok || !res.body) {
                update(id, t => endTest(t, `The test could not start (${res.status}).`));
                return;
            }
            const reader = res.body.getReader();
            const decoder = new TextDecoder();
            let buffer = '';
            for (;;) {
                const { done, value } = await readWithStallTimeout(reader, controller);
                if (done) break;
                buffer += decoder.decode(value, { stream: true });
                const lines = buffer.split('\n');
                buffer = lines.pop() || '';
                for (const line of lines) {
                    if (!line.startsWith('data: ')) continue;
                    let event: TestEvent;
                    try { event = JSON.parse(line.slice(6)); } catch { continue; }
                    update(id, t => reduceTest(t, event));
                }
            }
            update(id, t => endTest(t, 'The connection to this test ended before it answered.'));
        } catch {
            const reason = controller.signal.aborted ? controller.signal.reason : null;
            // Cleared, or the page left: the turn is no longer on screen.
            if (reason === 'clear' || reason === 'leave') return;
            update(id, t => (reason === 'stop'
                ? (t.state === 'streaming' ? { ...t, status: '', state: 'cancelled' } : t)
                : endTest(t, 'The connection to this test was lost.')));
        } finally {
            if (abort.current === controller) abort.current = null;
            setBusy(false);
        }
    };

    const stop = () => {
        // The turn runs on the server whether or not this page reads it; the
        // cancel is what stops it, and the abort only stops reading.
        void fetch(`/api/chat/${encodeURIComponent(session.current)}/cancel`, { method: 'POST' }).catch(() => {});
        abort.current?.abort('stop');
    };

    const name = agent?.name || 'this agent';
    const chatLink = agentId ? (
        <Link
            href="/"
            onClick={(e) => {
                // Chat opens this conversation in a tab, or brings its tab forward (TABS1). Read at the
                // click: the test's conversation is a ref, and a new test moves it without a render.
                e.preventDefault();
                router.push(`/?session=${encodeURIComponent(session.current)}&agent=${encodeURIComponent(agentId)}`);
            }}
            className="font-medium text-accent-ink underline underline-offset-2"
        >
            Chat
        </Link>
    ) : null;

    return (
        <div className={className}>
            <AgentTestPanel
                className="h-full"
                name={name}
                context={agent ? agentContext({ model: agent.model, tools: toolNames, knowledge: knowledgeNames }) : 'Not saved yet'}
                turns={turns}
                busy={busy}
                input={input}
                onInput={setInput}
                onSend={() => { void send(); }}
                onStop={stop}
                onClear={clear}
                dirty={dirty}
                onSave={onSave}
                saving={saving}
                blocked={agent ? undefined : 'Save the agent to try it.'}
                onCollapse={onCollapse}
                chatLink={chatLink}
                hasAttachment={attachments.images.length > 0}
                dropHandlers={attachments.dropHandlers}
                pasteHandlers={attachments.pasteHandlers}
                top={(
                    <>
                        <AttachmentError error={attachments.error} onDismiss={attachments.dismissError} />
                        <AttachmentChips images={attachments.images} onRemove={attachments.remove} />
                    </>
                )}
                attach={(
                    <AttachButton
                        onFiles={attachments.accept}
                        count={attachments.images.length}
                        disabledReason={refusal}
                        className="rounded-md p-1.5"
                    />
                )}
            />
        </div>
    );
}

/** The saved agent's tools as a person reads them, for the panel's one-line summary. */
export function toolNamesOf(agent: any): string[] | 'all' {
    const tools: string[] = Array.isArray(agent?.tools) ? agent.tools : [];
    if (tools.includes('all')) return 'all';
    return tools.map(t => humanizeToolName(t.replace(/^group:/, '')));
}
