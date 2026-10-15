'use client';
/**
 * The AI builder's conversation, as a slide-over on every screen.
 *
 * It renders whatever `lib/builderEvents` made of the run — it does no parsing
 * of its own. The state lives in `BuilderProvider` so the conversation survives
 * navigating between screens, which is the whole reason the builder moved out
 * of the orchestration editor.
 */
import { useEffect, useRef, useState } from 'react';
import {
    CheckCircle2,
    ChevronDown,
    ChevronUp,
    Trash2,
    Wrench,
    X,
} from 'lucide-react';

import {
    AGENT_BUILDER_SWITCH,
    AgentTurn,
    Composer,
    composerPillClass,
    ConnectCard,
    FindingsCard,
    PillAvatar,
    PlanCard,
    QuestionCard,
    questionKind,
    RunChip,
    Suggestions,
    UserBubble,
} from '@/components/assistant';
import { loadRunEstimate } from '@/lib/estimate';
import { BUILDER_EXAMPLES, BUILDER_GREETING, useBuilder } from '@/components/app/BuilderProvider';
import { HowImBuilt } from '@/components/app/HowImBuilt';
import { selfHostedConnector } from '@/components/app/builderConnector';
import {
    AttachButton,
    AttachmentChips,
    AttachmentError,
    useImageAttachments,
} from '@/components/app/ImageAttachments';
import { Combobox, Hint, Mark, Markdown, Spinner } from '@/components/ui';
import type { BuilderMessage } from '@/lib/builderEvents';
import { cn, focusRing } from '@/lib/cn';
import { modelOptions, withUnreachable } from '@/lib/models';

/** Empty `sessionModel` means "follow the workspace builder model". */
const DEFAULT_SENTINEL = '__default__';


function isActivity(message: BuilderMessage): boolean {
    return (
        message.kind === 'step_progress' ||
        message.kind === 'tool_call' ||
        message.kind === 'tool_result' ||
        message.kind === 'routing'
    );
}

/**
 * The mechanics of a build — steps, tool calls, routing — collapsed into one
 * block. Live it shows everything; finished it shows the last line with the
 * rest a click away, because the conversation is what the user came for.
 */
function ActivityGroup({ events, isLive }: { events: BuilderMessage[]; isLive: boolean }) {
    const [expanded, setExpanded] = useState(false);
    const earlier = events.slice(0, -1);
    const latest = events[events.length - 1];

    const renderEvent = (message: BuilderMessage, i: number) => {
        if (message.kind === 'step_progress') {
            const done = message.stepStatus === 'complete';
            return (
                <div key={i} className="flex items-center gap-2 py-0.5 pl-1">
                    {done
                        ? <CheckCircle2 size={14} className="shrink-0 text-success" aria-hidden />
                        : <Spinner size={16} className="shrink-0 text-machine" />}
                    <span className={cn('truncate text-xs', done ? 'text-text-faint' : 'text-text-muted')}>
                        {message.stepName}
                    </span>
                </div>
            );
        }
        if (message.kind === 'tool_call') {
            return (
                <div key={i} className="py-0.5">
                    <RunChip
                        icon={<Wrench size={13} />}
                        label={<span className="font-code">{message.toolName?.replace(/_/g, ' ')}</span>}
                        detail={(
                            <pre className="max-h-28 overflow-x-auto whitespace-pre-wrap rounded-md border border-border bg-surface-2 p-2 font-code text-2xs text-text-muted">
                                {JSON.stringify(message.toolArgs, null, 2)}
                            </pre>
                        )}
                    />
                </div>
            );
        }
        if (message.kind === 'tool_result') {
            const preview = (message.toolPreview || '').slice(0, 180);
            const truncated = (message.toolPreview || '').length > 180;
            return (
                <div key={i} className="truncate py-0.5 pl-6 font-code text-2xs text-text-faint">
                    ↳ {preview}{truncated ? '…' : ''}
                </div>
            );
        }
        if (message.kind === 'routing' && message.routingDecision) {
            return (
                <div key={i} className="flex items-center gap-1.5 py-0.5 pl-1">
                    <span className="text-xs text-text-faint" aria-hidden>→</span>
                    <span className="font-code text-2xs text-text-muted">{message.routingDecision}</span>
                </div>
            );
        }
        return null;
    };

    return (
        <div className="space-y-0.5">
            {(isLive || expanded) && earlier.map(renderEvent)}
            {!isLive && earlier.length > 0 && (
                <button
                    type="button"
                    onClick={() => setExpanded((v) => !v)}
                    className={cn('flex items-center gap-1 pl-1 text-xs text-text-faint transition-colors hover:text-text-muted', focusRing)}
                >
                    {expanded
                        ? <><ChevronUp size={10} aria-hidden /> hide</>
                        : <><ChevronDown size={10} aria-hidden /> {earlier.length} earlier step{earlier.length !== 1 ? 's' : ''}</>}
                </button>
            )}
            {latest && renderEvent(latest, earlier.length)}
        </div>
    );
}

export function BuilderPanel() {
    const {
        isOpen, entered, close, state, streaming, send, stop, newConversation, canAttach,
        providers, builderModel, sessionModel, setSessionModel, saveBuilderModel, draft,
    } = useBuilder();

    const [input, setInput] = useState('');
    // Words handed over by `open({ prompt })` land in the composer, unsent, for
    // the person to read and send. Adopted while rendering, not in an effect.
    const [adopted, setAdopted] = useState(draft?.n ?? 0);
    if (draft && draft.n !== adopted) {
        setAdopted(draft.n);
        setInput(draft.text);
    }
    const [how, setHow] = useState(false);
    const bottomRef = useRef<HTMLDivElement>(null);
    // The composer's field, for "Change something" and a suggestion to focus.
    const textarea = useRef<HTMLTextAreaElement>(null);
    const attachments = useImageAttachments();

    useEffect(() => {
        // Not while closing: scrolling a card that is fading out is motion in
        // two directions at once.
        if (!entered) return;
        // `nearest`: the default (`start`) also scrolls every ancestor that can
        // scroll — the card itself among them, though it clips — so a long
        // message or a pasted export pushed the panel's header out of sight.
        bottomRef.current?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }, [state.messages, entered]);

    // `isOpen` stays true for the length of the exit transition — the provider
    // clears it when the card has finished leaving.
    if (!isOpen) return null;

    // Working, not asking: the one composer button is Stop, as it is in chat.
    // A run parked on a question is not streaming here — the stream ends at the
    // pause — so that case is the composer's Send plus the card's own way out.
    const working = streaming && Boolean(state.runId);
    const openQuestionKey = state.pending
        ? state.messages.findLast((m) => m.kind === 'human_prompt')?.key
        : undefined;

    const submit = () => {
        const text = input.trim();
        // An image on its own is a message — "what is this?" is often the
        // picture, not the words.
        if ((!text && !attachments.images.length) || streaming) return;
        const images = attachments.images.map((image) => image.base64);
        setInput('');
        attachments.clear();
        void send(text, images);
    };

    // Grouped by provider rather than one flat list of ~66, and a remembered
    // pick whose provider has since lost its key shows under "Not reachable"
    // instead of leaving the control looking empty.
    const modelChoices = [
        { value: DEFAULT_SENTINEL, label: `Default (${builderModel || 'workspace model'})` },
        ...withUnreachable(modelOptions(providers), sessionModel || undefined),
    ];

    // Consecutive activity collapses into one block; everything else is its own.
    type Group = { type: 'activity'; events: BuilderMessage[] } | { type: 'single'; message: BuilderMessage };
    const groups: Group[] = [];
    let buffer: BuilderMessage[] = [];
    for (const message of state.messages) {
        if (isActivity(message)) {
            buffer.push(message);
        } else {
            if (buffer.length) { groups.push({ type: 'activity', events: buffer }); buffer = []; }
            groups.push({ type: 'single', message });
        }
    }
    if (buffer.length) groups.push({ type: 'activity', events: buffer });

    const ask = state.pending ? 'Answer the question above…' : 'Describe what you want to happen…';
    const fresh = state.messages.every((m) => m.key === 'welcome') && !streaming;
    return (
        <>
            <aside
                aria-label="AI builder"
                onKeyDown={(e) => {
                    // Escape on the panel, not on the document: the card is not
                    // modal and does not trap focus, so a document listener
                    // would eat Escape from whatever screen is behind it.
                    if (e.key === 'Escape') { e.stopPropagation(); close(); }
                }}
                className={cn(
                    'fixed right-3 top-3 bottom-3 z-40 flex flex-col overflow-hidden',
                    // `min()` rather than `max-w`: the card keeps its 12px of
                    // margin on both sides all the way down to a 320px phone,
                    // where it is 296px wide and the page still does not
                    // scroll sideways.
                    'w-[min(620px,calc(100vw-1.5rem))]',
                    // `overflow-hidden` is what gives the header and the
                    // composer the inner radius, instead of restating it on
                    // three children that would then disagree.
                    'rounded-2xl border border-border-strong bg-bg shadow-elevated',
                    'transition-[opacity,transform] duration-200 ease-out motion-reduce:transition-none',
                    entered ? 'translate-x-0 opacity-100' : 'pointer-events-none translate-x-2 opacity-0',
                )}
            >
                <header className="flex shrink-0 items-center justify-between border-b border-border px-4 py-3">
                    <div className="flex items-center gap-2">
                        <Mark size={20} compact={false} className="text-accent" aria-hidden />
                        <span className="text-sm font-semibold text-text">AI Builder</span>
                    </div>
                    <div className="flex items-center gap-1">
                        <button
                            type="button"
                            onClick={() => setHow(true)}
                            className={cn('rounded-md px-2 py-1 text-xs text-text-muted transition-colors hover:bg-surface-2 hover:text-text', focusRing)}
                        >
                            See how I&rsquo;m built
                        </button>
                        <Hint content="Start a new conversation">
                            <button
                                type="button"
                                onClick={newConversation}
                                aria-label="Start a new conversation"
                                className={cn('rounded-md p-1.5 text-text-faint transition-colors hover:bg-surface-2 hover:text-text', focusRing)}
                            >
                                <Trash2 size={14} aria-hidden />
                            </button>
                        </Hint>
                        <button
                            type="button"
                            onClick={close}
                            aria-label="Close the AI builder"
                            className={cn('rounded-md p-1.5 text-text-faint transition-colors hover:bg-surface-2 hover:text-text', focusRing)}
                        >
                            <X size={16} aria-hidden />
                        </button>
                    </div>
                </header>

                <div className="flex flex-1 flex-col gap-4 overflow-y-auto overscroll-contain px-4 py-5">
                    {groups.map((group, i) => {
                        if (group.type === 'activity') {
                            return (
                                <div key={`activity-${i}`} className="pl-10">
                                    <ActivityGroup events={group.events} isLive={streaming && i === groups.length - 1} />
                                </div>
                            );
                        }
                        const { message } = group;

                        // The greeting: the mark and what the builder does. Its examples
                        // are suggestions under the empty composer.
                        if (message.key === 'welcome') {
                            return (
                                <div key="welcome" className="flex flex-col items-center gap-2 px-2 pb-1 pt-4 text-center">
                                    <Mark size={36} compact={false} className="text-accent" aria-hidden />
                                    <h2 className="mt-1 text-xl font-semibold tracking-tight text-text">{BUILDER_GREETING.title}</h2>
                                    <p className="max-w-[44ch] text-sm leading-relaxed text-text-muted">{BUILDER_GREETING.text}</p>
                                </div>
                            );
                        }
                        if (message.kind === 'banner') {
                            const gone = message.saved?.action === 'deleted';
                            return (
                                <div
                                    key={message.key}
                                    className={cn(
                                        'ml-10 flex items-center gap-2 rounded-lg border px-3 py-2 text-[13px]',
                                        gone
                                            ? 'border-border bg-surface-2 text-text-muted'
                                            : 'border-success/40 bg-success-subtle text-text',
                                    )}
                                >
                                    <CheckCircle2 size={14} className={cn('shrink-0', gone ? 'text-text-faint' : 'text-success')} aria-hidden />
                                    <span className="truncate">{message.content}</span>
                                </div>
                            );
                        }
                        if (message.kind === 'connect' && message.connect) {
                            return (
                                <AgentTurn key={message.key} density="panel" avatar={<Mark size={20} compact={false} className="text-accent" aria-hidden />}>
                                    <ConnectCard
                                        spec={message.connect}
                                        connector={selfHostedConnector}
                                        onConnected={(text) => void send(text, [])}
                                    />
                                </AgentTurn>
                            );
                        }
                        if (message.kind === 'findings' && message.findings) {
                            return (
                                <AgentTurn key={message.key} density="panel" avatar={<Mark size={20} compact={false} className="text-accent" aria-hidden />}>
                                    <FindingsCard findings={message.findings} />
                                </AgentTurn>
                            );
                        }
                        if (message.kind === 'human_prompt') {
                            const open = message.key === openQuestionKey;
                            const kind = questionKind(message.stepId, message.content);
                            const onStop = open ? () => void stop() : undefined;
                            return (
                                <AgentTurn key={message.key} density="panel" avatar={<Mark size={20} compact={false} className="text-accent" aria-hidden />}>
                                    {kind === 'build' || kind === 'change' ? (
                                        <PlanCard
                                            prompt={message.content}
                                            kind={kind}
                                            open={open}
                                            onAnswer={(text) => void send(text, [])}
                                            onRevise={() => textarea.current?.focus()}
                                            onStop={onStop}
                                            estimate={loadRunEstimate}
                                        />
                                    ) : (
                                        <QuestionCard content={message.content} open={open} onStop={onStop} />
                                    )}
                                </AgentTurn>
                            );
                        }
                        if (message.role === 'user') {
                            return (
                                <UserBubble key={message.key} density="panel">
                                    <span className="whitespace-pre-wrap">{message.content}</span>
                                </UserBubble>
                            );
                        }
                        return (
                            <AgentTurn key={message.key} density="panel" avatar={<Mark size={20} compact={false} className="text-accent" aria-hidden />}>
                                <Markdown className="text-sm leading-relaxed">{message.content}</Markdown>
                            </AgentTurn>
                        );
                    })}

                    {streaming && state.status && (
                        // Working: the mark in the avatar's slot, and what it is doing.
                        <AgentTurn working size={20} density="panel">
                            <span className="text-xs text-text-muted">{state.status}</span>
                        </AgentTurn>
                    )}
                    <div ref={bottomRef} />
                </div>

                <div className="shrink-0 space-y-2 border-t border-border bg-bg px-3 pb-3 pt-3">
                    <AttachmentError error={attachments.error} onDismiss={attachments.dismissError} />
                    <Composer
                        density="panel"
                        value={input}
                        onChange={setInput}
                        onSend={submit}
                        canSend={(Boolean(input.trim()) || attachments.images.length > 0) && !streaming}
                        sendLabel={state.pending ? 'Send your answer' : 'Send'}
                        // Only until the run has an id — after that this button is Stop.
                        sendPending={streaming && !working}
                        busy={working}
                        onStop={() => void stop()}
                        stopLabel="Stop the builder"
                        stopTitle="Stop"
                        placeholder={ask}
                        label="Message the AI builder"
                        disabled={streaming}
                        textareaRef={textarea}
                        dropHandlers={attachments.dropHandlers}
                        pasteHandlers={attachments.pasteHandlers}
                        top={<AttachmentChips images={attachments.images} onRemove={attachments.remove} />}
                        pill={(
                            <span className={cn(composerPillClass, 'hover:bg-surface-2')}>
                                <PillAvatar><Mark size={14} className="text-accent" aria-hidden /></PillAvatar>
                                AI builder
                            </span>
                        )}
                        attach={(
                            <AttachButton
                                onFiles={attachments.accept}
                                count={attachments.images.length}
                                disabledReason={canAttach ? '' : 'This model cannot read images'}
                                className="rounded-md p-1.5"
                            />
                        )}
                        model={(
                            <>
                                {sessionModel && sessionModel !== builderModel && (
                                    <button
                                        type="button"
                                        onClick={() => void saveBuilderModel(sessionModel)}
                                        className={cn('shrink-0 rounded-md px-1.5 py-0.5 text-xs text-accent-ink transition-colors hover:bg-accent-subtle', focusRing)}
                                    >
                                        Set as default
                                    </button>
                                )}
                                <Combobox
                                    value={sessionModel || DEFAULT_SENTINEL}
                                    onChange={(v) => setSessionModel(v === DEFAULT_SENTINEL ? '' : v)}
                                    aria-label="Model for this build"
                                    searchPlaceholder="Search models…"
                                    size="sm"
                                    trigger="pill"
                                    className="max-w-[200px]"
                                    options={modelChoices}
                                />
                            </>
                        )}
                        hint="Enter to send · Shift+Enter for a newline"
                        showHint={state.messages.every((m) => m.role !== 'user')}
                    />
                    {fresh && (
                        <Suggestions
                            items={[...BUILDER_EXAMPLES, AGENT_BUILDER_SWITCH.suggestion]}
                            label="Things to ask for"
                            onPick={(text) => {
                                // The switch's chip says what it is for; the composer gets
                                // the lead line its export goes under (SW1).
                                setInput(text === AGENT_BUILDER_SWITCH.suggestion ? AGENT_BUILDER_SWITCH.lead : text);
                                textarea.current?.focus();
                            }}
                            className="pt-1"
                        />
                    )}
                </div>
            </aside>

            <HowImBuilt
                open={how}
                onClose={() => setHow(false)}
                onDuplicated={(id) => { window.location.href = `/workflows?id=${id}`; }}
            />
        </>
    );
}
