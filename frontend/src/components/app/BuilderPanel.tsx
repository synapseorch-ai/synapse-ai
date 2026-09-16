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
    Send,
    Square,
    Trash2,
    X,
} from 'lucide-react';

import { useBuilder } from '@/components/app/BuilderProvider';
import { HowImBuilt } from '@/components/app/HowImBuilt';
import {
    AttachButton,
    AttachmentChips,
    AttachmentError,
    useImageAttachments,
} from '@/components/app/ImageAttachments';
import { Combobox, Hint, Mark, Spinner } from '@/components/ui';
import type { BuilderMessage } from '@/lib/builderEvents';
import { cn, focusRing } from '@/lib/cn';
import { modelOptions, withUnreachable } from '@/lib/models';
import { renderTextContent } from '@/lib/utils';

const COLLAPSE_THRESHOLD = 600;

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
                        ? <CheckCircle2 size={12} className="shrink-0 text-success" aria-hidden />
                        : <Spinner size={16} className="shrink-0 text-accent" />}
                    <span className={cn('truncate font-code text-[11px]', done ? 'text-text-faint' : 'text-text-muted')}>
                        {message.stepName}
                    </span>
                </div>
            );
        }
        if (message.kind === 'tool_call') {
            return (
                <div key={i} className="flex items-start gap-2 py-0.5 pl-1">
                    <span className="mt-0.5 shrink-0 text-[10px] text-accent" aria-hidden>🔧</span>
                    <details className="min-w-0 flex-1">
                        <summary className="cursor-pointer list-none truncate font-code text-[11px] text-accent">
                            {message.toolName?.replace(/_/g, ' ')}
                        </summary>
                        <pre className="mt-0.5 max-h-28 overflow-x-auto whitespace-pre-wrap rounded-md border border-border bg-surface-2 p-1.5 font-code text-[10px] text-text-muted">
                            {JSON.stringify(message.toolArgs, null, 2)}
                        </pre>
                    </details>
                </div>
            );
        }
        if (message.kind === 'tool_result') {
            const preview = (message.toolPreview || '').slice(0, 180);
            const truncated = (message.toolPreview || '').length > 180;
            return (
                <div key={i} className="truncate py-0.5 pl-7 font-code text-[10px] text-text-faint">
                    ↳ {preview}{truncated ? '…' : ''}
                </div>
            );
        }
        if (message.kind === 'routing' && message.routingDecision) {
            return (
                <div key={i} className="flex items-center gap-1.5 py-0.5 pl-1">
                    <span className="text-[10px] text-text-faint" aria-hidden>→</span>
                    <span className="font-code text-[10px] text-warning">{message.routingDecision}</span>
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
                    className={cn('flex items-center gap-1 pl-1 text-[10px] text-text-faint transition-colors hover:text-text-muted', focusRing)}
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

/**
 * A question the run is waiting on — usually the plan, which runs long.
 *
 * `onStop` on the question still open, and only there. The composer below is
 * for the answer, so the other way out of a question belongs on the question —
 * the same pair the run page's own card offers. Without it a build parked on a
 * question can only be answered, and walking away leaves it paused for good.
 */
function HumanPromptCard({ content, onStop }: { content: string; onStop?: () => void }) {
    const [expanded, setExpanded] = useState(false);
    const isLong = content.length > COLLAPSE_THRESHOLD;
    const visible = isLong && !expanded ? `${content.slice(0, COLLAPSE_THRESHOLD)}…` : content;

    return (
        <div className="overflow-hidden rounded-lg border border-warning/40 bg-warning-subtle/40">
            <div className="border-b border-warning/30 px-3 py-1.5">
                <span className="font-code text-[9px] uppercase tracking-widest text-warning">Needs your answer</span>
            </div>
            <div className="px-3 py-2.5 text-[12px] leading-relaxed text-text">
                {renderTextContent(visible)}
            </div>
            {isLong && (
                <button
                    type="button"
                    onClick={() => setExpanded((v) => !v)}
                    className={cn('flex w-full items-center justify-center gap-1 border-t border-border py-1.5 text-[10px] text-text-faint transition-colors hover:text-text', focusRing)}
                >
                    {expanded
                        ? <><ChevronUp size={10} aria-hidden /> Show less</>
                        : <><ChevronDown size={10} aria-hidden /> Show all of it</>}
                </button>
            )}
            {onStop && (
                <div className="flex items-center justify-end border-t border-warning/30 px-3 py-1.5">
                    <button
                        type="button"
                        onClick={onStop}
                        className={cn(
                            'rounded-md px-2 py-0.5 text-[11px] text-danger transition-colors hover:bg-danger-subtle',
                            focusRing,
                        )}
                    >
                        Stop instead
                    </button>
                </div>
            )}
        </div>
    );
}

export function BuilderPanel() {
    const {
        isOpen, entered, close, state, streaming, send, stop, newConversation, canAttach,
        providers, builderModel, sessionModel, setSessionModel, saveBuilderModel,
    } = useBuilder();

    const [input, setInput] = useState('');
    const [how, setHow] = useState(false);
    const bottomRef = useRef<HTMLDivElement>(null);
    const attachments = useImageAttachments();

    useEffect(() => {
        // Not while closing: scrolling a card that is fading out is motion in
        // two directions at once.
        if (!entered) return;
        bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
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
                    'rounded-xl border border-border-strong bg-surface shadow-2xl',
                    'transition-[opacity,transform] duration-200 ease-out motion-reduce:transition-none',
                    entered ? 'translate-x-0 opacity-100' : 'pointer-events-none translate-x-2 opacity-0',
                )}
            >
                <header className="flex shrink-0 items-center justify-between border-b border-border px-5 py-3.5">
                    <div className="flex items-center gap-2">
                        <Mark size={20} compact={false} className="text-accent" aria-hidden />
                        <span className="text-sm font-semibold text-text">AI Builder</span>
                    </div>
                    <div className="flex items-center gap-1">
                        <button
                            type="button"
                            onClick={() => setHow(true)}
                            className={cn('rounded-md px-2 py-1 text-[11px] text-text-faint transition-colors hover:text-text', focusRing)}
                        >
                            See how I&rsquo;m built
                        </button>
                        <Hint content="Start a new conversation">
                            <button
                                type="button"
                                onClick={newConversation}
                                aria-label="Start a new conversation"
                                className={cn('rounded-md p-1 text-text-faint transition-colors hover:text-text', focusRing)}
                            >
                                <Trash2 size={14} aria-hidden />
                            </button>
                        </Hint>
                        <button
                            type="button"
                            onClick={close}
                            aria-label="Close the AI builder"
                            className={cn('rounded-md p-1 text-text-faint transition-colors hover:text-text', focusRing)}
                        >
                            <X size={16} aria-hidden />
                        </button>
                    </div>
                </header>

                <div className="flex-1 space-y-3 overflow-y-auto overscroll-contain px-5 py-4 font-sans">
                    {groups.map((group, i) => {
                        if (group.type === 'activity') {
                            return (
                                <ActivityGroup
                                    key={`activity-${i}`}
                                    events={group.events}
                                    isLive={streaming && i === groups.length - 1}
                                />
                            );
                        }
                        const { message } = group;

                        if (message.kind === 'banner') {
                            const gone = message.saved?.action === 'deleted';
                            return (
                                <div
                                    key={message.key}
                                    className={cn(
                                        'flex items-center gap-2 rounded-lg border px-3 py-2 text-[12px]',
                                        gone
                                            ? 'border-border bg-surface-2 text-text-muted'
                                            : 'border-success/40 bg-success-subtle text-text',
                                    )}
                                >
                                    <CheckCircle2 size={13} className={cn('shrink-0', gone ? 'text-text-faint' : 'text-success')} aria-hidden />
                                    <span className="truncate">{message.content}</span>
                                </div>
                            );
                        }
                        if (message.kind === 'human_prompt') {
                            return (
                                <HumanPromptCard
                                    key={message.key}
                                    content={message.content}
                                    onStop={message.key === openQuestionKey ? () => void stop() : undefined}
                                />
                            );
                        }
                        if (message.role === 'user') {
                            return (
                                <div key={message.key} className="flex justify-end">
                                    <div className="max-w-[80%] whitespace-pre-wrap rounded-xl bg-accent px-4 py-2.5 text-sm text-accent-fg">
                                        {message.content}
                                    </div>
                                </div>
                            );
                        }
                        return (
                            <div key={message.key} className="flex justify-start">
                                <div className="max-w-[88%] rounded-xl bg-surface-2 px-4 py-3 text-sm text-text">
                                    {renderTextContent(message.content)}
                                </div>
                            </div>
                        );
                    })}

                    {streaming && state.status && (
                        <div className="flex items-center gap-2 pl-1">
                            <span className="h-1.5 w-1.5 shrink-0 animate-pulse rounded-full bg-accent" aria-hidden />
                            <span className="font-code text-[11px] text-text-faint">{state.status}</span>
                        </div>
                    )}
                    <div ref={bottomRef} />
                </div>

                <div className="shrink-0 space-y-2.5 border-t border-border px-4 pb-4 pt-3">
                    <AttachmentError error={attachments.error} onDismiss={attachments.dismissError} />

                    {/* One bordered box holding the chips and the row, so an
                        attachment reads as part of the message being written
                        rather than as a notification above it. */}
                    <div
                        className={cn(
                            'flex flex-col rounded-lg border border-border-strong bg-surface-2',
                            'transition-colors focus-within:border-accent',
                        )}
                        {...attachments.dropHandlers}
                    >
                        <AttachmentChips images={attachments.images} onRemove={attachments.remove} />
                        <div className="flex items-end gap-1">
                            <textarea
                                value={input}
                                onChange={(e) => setInput(e.target.value)}
                                onKeyDown={(e) => {
                                    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); submit(); }
                                }}
                                placeholder={state.pending ? 'Answer the question above…' : 'Describe what you want to happen…'}
                                rows={2}
                                disabled={streaming}
                                aria-label="Message the AI builder"
                                // The box around this owns the focus ring; see
                                // `[data-no-focus-ring]` in globals.css.
                                data-no-focus-ring
                                {...attachments.pasteHandlers}
                                className={cn(
                                    'flex-1 resize-none bg-transparent px-3.5 py-2.5',
                                    'font-sans text-sm text-text placeholder:text-text-faint',
                                    'outline-none disabled:opacity-50',
                                )}
                            />
                            <AttachButton
                                onFiles={attachments.accept}
                                count={attachments.images.length}
                                disabledReason={canAttach ? '' : 'This model cannot read images'}
                                className="mb-2 p-1.5"
                            />
                            {/* One button, in whichever job the moment calls
                                for — the chat composer's rule. Two would mean
                                one of them is always dead. */}
                            {working ? (
                                <button
                                    type="button"
                                    onClick={() => void stop()}
                                    aria-label="Stop the builder"
                                    title="Stop"
                                    className={cn(
                                        'mb-1.5 mr-1.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-md',
                                        'border border-border-strong bg-surface text-text',
                                        'transition-colors hover:border-text-faint hover:bg-surface-2',
                                        focusRing,
                                    )}
                                >
                                    <Square size={12} className="fill-current" aria-hidden />
                                </button>
                            ) : (
                                <button
                                    type="button"
                                    onClick={submit}
                                    disabled={(!input.trim() && !attachments.images.length) || streaming}
                                    aria-label={state.pending ? 'Send your answer' : 'Send'}
                                    className={cn(
                                        'mb-1.5 mr-1.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-accent text-accent-fg',
                                        'transition-colors hover:bg-accent-hover disabled:cursor-not-allowed disabled:opacity-40',
                                        focusRing,
                                    )}
                                >
                                    {/* Only until the run has an id — after that
                                        this button is Stop. */}
                                    {streaming ? <Spinner size={16} /> : <Send size={15} aria-hidden />}
                                </button>
                            )}
                        </div>
                    </div>

                    <div className="flex items-center gap-1.5">
                        <span className="shrink-0 text-[10px] text-text-faint">Model</span>
                        <Combobox
                            value={sessionModel || DEFAULT_SENTINEL}
                            onChange={(v) => setSessionModel(v === DEFAULT_SENTINEL ? '' : v)}
                            aria-label="Model for this build"
                            searchPlaceholder="Search models…"
                            size="sm"
                            className="max-w-[200px]"
                            options={modelChoices}
                        />
                        {sessionModel && sessionModel !== builderModel && (
                            <button
                                type="button"
                                onClick={() => void saveBuilderModel(sessionModel)}
                                className={cn('rounded-md px-1.5 py-0.5 text-[10px] text-accent transition-colors hover:text-accent-hover', focusRing)}
                            >
                                Set as default
                            </button>
                        )}
                        {/* Wraps to three lines and squeezes the picker on a phone-width
                            card, where it is also the least useful thing on screen. */}
                        <span className="ml-auto hidden text-[10px] text-text-faint sm:inline">Enter to send · Shift+Enter for a newline</span>
                    </div>
                </div>
            </aside>

            <HowImBuilt
                open={how}
                onClose={() => setHow(false)}
                onDuplicated={(id) => { window.location.href = `/orchestrations?id=${id}`; }}
            />
        </>
    );
}
