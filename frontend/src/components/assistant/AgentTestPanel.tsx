'use client';

/*
 * The agents workbench's test chat (M2d, D211): a short conversation with the
 * agent as it is saved, beside its setup.
 *
 * It talks to the saved agent, never to the edits on screen: testing unsaved
 * settings would need the engine to take an agent that is not stored (D211,
 * Naveen: "let it be saved things we can chat"). So while there are edits the
 * panel says so and offers to save first; sending still works, against what is
 * saved.
 *
 * Shared by both products. Each brings its transport (`onSend`, `onStop`), its
 * Attach (`attach`, `top`, the drop and paste handlers) and its link to a
 * conversation in chat (`chatLink`); the turns themselves are `testChat.ts`.
 */
import { Bot, PanelRightClose, RotateCcw, Wrench } from 'lucide-react';
import * as React from 'react';

import { KnowledgeSources, linkLabels } from '@/components/knowledge/KnowledgeSources';
import { Button, IconButton, Markdown } from '@/components/ui';
import { cn } from '@/lib/cn';

import { Composer, composerPillClass, PillAvatar } from './Composer';
import { AgentAvatar, AgentTurn, RunChip, UserBubble } from './messages';
import type { TestCall, TestTurn } from './testChat';

export interface AgentTestPanelProps {
    /** The saved agent's name. */
    name: string;
    /** The saved agent in one line (`agentContext`). */
    context: string;
    turns: TestTurn[];
    busy: boolean;
    input: string;
    onInput: (value: string) => void;
    onSend: () => void;
    onStop: () => void;
    /** Start a new test conversation. */
    onClear: () => void;
    /** The setup has edits that are not saved. */
    dirty: boolean;
    /** Save the edits; the panel then tests what was saved. Absent where saving is not allowed. */
    onSave?: () => void;
    saving?: boolean;
    /**
     * Why there is no test to run, in place of the composer: the agent is not
     * saved yet, or this person may not chat ("Only builders can test agents").
     */
    blocked?: string;
    /** Hide the panel; the setup takes its room. Absent in the phone's drawer, which closes itself. */
    onCollapse?: () => void;
    /** A link to this test's conversation in chat, for a turn that waits on a person. */
    chatLink?: React.ReactNode;
    /** The product's Attach button and its chips. */
    attach?: React.ReactNode;
    top?: React.ReactNode;
    /** Something besides words to send (an attached image). */
    hasAttachment?: boolean;
    dropHandlers?: React.ComponentProps<typeof Composer>['dropHandlers'];
    pasteHandlers?: React.ComponentProps<typeof Composer>['pasteHandlers'];
    /** The agent's icon for its turns and the pill. */
    icon?: React.ReactNode;
    className?: string;
}

export function AgentTestPanel({
    name, context, turns, busy, input, onInput, onSend, onStop, onClear, dirty, onSave, saving = false,
    blocked, onCollapse, chatLink, attach, top, hasAttachment = false, dropHandlers, pasteHandlers, icon, className,
}: AgentTestPanelProps) {
    const scroller = React.useRef<HTMLDivElement | null>(null);
    const glyph = icon ?? <Bot className="size-3.5" aria-hidden />;

    // Follow the conversation while it grows, as chat does.
    const last = turns[turns.length - 1];
    React.useEffect(() => {
        const el = scroller.current;
        if (el) el.scrollTop = el.scrollHeight;
    }, [turns.length, last?.calls.length, last?.answer, last?.state, last?.status]);

    return (
        <section aria-label={`Try ${name}`} className={cn('flex min-h-0 flex-col', className)}>
            <header className="flex items-start gap-2 border-b border-border px-4 py-3">
                <div className="min-w-0 flex-1">
                    <h2 className="truncate text-sm font-semibold text-text">Try {name}</h2>
                    <p className="truncate text-xs text-text-faint" title={context}>{context}</p>
                </div>
                <IconButton size="sm" icon={RotateCcw} label="Clear the test" onClick={onClear} disabled={busy || turns.length === 0} />
                {onCollapse && <IconButton size="sm" icon={PanelRightClose} label="Hide the test" onClick={onCollapse} />}
            </header>

            {dirty && !blocked && (
                <div role="status" className="flex flex-wrap items-center gap-x-3 gap-y-2 border-b border-border bg-warning-subtle px-4 py-2.5 text-xs text-text">
                    <span className="min-w-0 flex-1">Testing the saved version. Your unsaved changes aren&apos;t in it yet.</span>
                    {onSave && (
                        <Button size="sm" variant="secondary" onClick={onSave} loading={saving}>Save and test</Button>
                    )}
                </div>
            )}

            <div ref={scroller} className="flex min-h-0 flex-1 flex-col gap-5 overflow-y-auto px-4 py-4">
                {turns.length === 0 && !blocked && (
                    <p className="m-auto max-w-[32ch] text-center text-sm text-text-faint">
                        Ask {name} something to see what it does with it: the tools it calls and the answer it gives.
                    </p>
                )}
                {turns.map((turn) => (
                    <TurnView key={turn.id} turn={turn} glyph={glyph} chatLink={chatLink} />
                ))}
            </div>

            <div className="border-t border-border px-3 pb-3 pt-3">
                {blocked ? (
                    <p className="px-1 py-2 text-sm text-text-muted">{blocked}</p>
                ) : (
                    <Composer
                        density="panel"
                        rows={1}
                        value={input}
                        onChange={onInput}
                        onSend={onSend}
                        canSend={!busy && (input.trim().length > 0 || hasAttachment)}
                        sendLabel="Send"
                        busy={busy}
                        onStop={onStop}
                        stopLabel="Stop the test"
                        placeholder={`Message ${name}…`}
                        label={`Message ${name}`}
                        pill={<span className={cn(composerPillClass, 'hover:bg-surface-2')}><PillAvatar>{glyph}</PillAvatar><span className="truncate">{name}</span></span>}
                        attach={attach}
                        top={top}
                        dropHandlers={dropHandlers}
                        pasteHandlers={pasteHandlers}
                    />
                )}
            </div>
        </section>
    );
}

function TurnView({ turn, glyph, chatLink }: { turn: TestTurn; glyph: React.ReactNode; chatLink?: React.ReactNode }) {
    const anchor = `test-${turn.id}`;
    const working = turn.state === 'streaming';
    return (
        <div className="flex min-w-0 flex-col gap-3">
            <UserBubble density="panel">
                {turn.question}
                {turn.images ? <span className="mt-1 block text-2xs text-text-faint">{turn.images} image{turn.images === 1 ? '' : 's'}</span> : null}
            </UserBubble>
            <AgentTurn density="panel" size={24} working={working} avatar={<AgentAvatar size={24}>{glyph}</AgentAvatar>}>
                {turn.calls.map((call) => <CallChip key={call.key} call={call} />)}
                {turn.answer && <Markdown className="text-sm">{linkLabels(turn.answer, turn.citations, anchor)}</Markdown>}
                {(turn.citations?.length || turn.unresolved?.length) ? (
                    <KnowledgeSources citations={turn.citations} unresolved={turn.unresolved} anchor={anchor} />
                ) : null}
                {working && <p className="text-xs text-text-faint" aria-live="polite">{turn.status || 'Working…'}</p>}
                {turn.state === 'waiting' && (
                    <p className="text-sm text-text-muted">
                        {turn.waiting === 'approval' ? 'A tool call is waiting for your permission.' : 'It is waiting for someone to answer a question.'}
                        {chatLink ? <> Answer it in {chatLink}.</> : null}
                    </p>
                )}
                {turn.state === 'cancelled' && <p className="text-xs text-text-faint">Stopped.</p>}
                {turn.state === 'failed' && <p role="alert" className="text-sm text-danger">{turn.error}</p>}
                {turn.state === 'done' && !turn.answer && <p className="text-xs text-text-faint">It ended without an answer.</p>}
            </AgentTurn>
        </div>
    );
}

function CallChip({ call }: { call: TestCall }) {
    const args = call.args && Object.keys(call.args).length > 0 ? JSON.stringify(call.args) : '';
    const label = (
        <>
            <span className="font-code text-text" translate="no">{call.name}</span>
            {call.result === null && <span className="text-text-faint"> · running</span>}
            {call.refused && <span className="text-warning"> · not run</span>}
            {call.failed && <span className="text-danger"> · failed</span>}
        </>
    );
    const detail = args || call.result || call.reason ? (
        <div className="space-y-1 border-l border-border pl-3 text-2xs text-text-faint">
            {args && <div className="break-all font-code" title={args}>{args.slice(0, 400)}</div>}
            {call.refused && call.reason && <div className="text-warning">{call.reason}</div>}
            {call.result && <div className="line-clamp-4 whitespace-pre-wrap break-words">{call.result.slice(0, 400)}</div>}
        </div>
    ) : undefined;
    return (
        <RunChip
            icon={<Wrench className="size-3.5" />}
            label={label}
            detail={detail}
            tone={call.failed ? 'danger' : 'default'}
        />
    );
}
