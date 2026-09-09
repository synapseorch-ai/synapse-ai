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
    Loader2,
    Send,
    Sparkles,
    Trash2,
    X,
} from 'lucide-react';

import { useBuilder } from '@/components/app/BuilderProvider';
import { HowImBuilt } from '@/components/app/HowImBuilt';
import { Combobox, Hint } from '@/components/ui';
import type { BuilderMessage } from '@/lib/builderEvents';
import { cn, focusRing } from '@/lib/cn';
import { renderTextContent } from '@/lib/utils';

const COLLAPSE_THRESHOLD = 600;

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
                        : <Loader2 size={12} className="shrink-0 animate-spin text-accent" aria-hidden />}
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
                        <pre className="mt-0.5 max-h-28 overflow-x-auto whitespace-pre-wrap rounded-md border border-border bg-surface p-1.5 font-code text-[10px] text-text-muted">
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

/** A question the run is waiting on — usually the plan, which runs long. */
function HumanPromptCard({ content }: { content: string }) {
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
        </div>
    );
}

export function BuilderPanel() {
    const {
        isOpen, close, state, streaming, send, newConversation,
        models, builderModel, sessionModel, setSessionModel, saveBuilderModel,
    } = useBuilder();

    const [input, setInput] = useState('');
    // Null when closed. The picture inside comes in two themes, and the theme is
    // read when the modal is opened rather than tracked — the app keys light
    // mode off a class on <html>, which is not something React can subscribe to.
    const [how, setHow] = useState<{ theme: 'light' | 'dark' } | null>(null);
    const bottomRef = useRef<HTMLDivElement>(null);

    useEffect(() => {
        bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
    }, [state.messages]);

    if (!isOpen) return null;

    const submit = () => {
        const text = input.trim();
        if (!text || streaming) return;
        setInput('');
        void send(text);
    };

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
                className="fixed inset-y-0 right-0 z-40 flex w-full max-w-[620px] flex-col border-l border-border bg-bg shadow-2xl"
            >
                <header className="flex shrink-0 items-center justify-between border-b border-border px-5 py-3.5">
                    <div className="flex items-center gap-2">
                        <Sparkles size={15} className="text-accent" aria-hidden />
                        <span className="text-sm font-semibold text-text">AI Builder</span>
                    </div>
                    <div className="flex items-center gap-1">
                        <button
                            type="button"
                            onClick={() => setHow({
                                theme: document.documentElement.classList.contains('light-mode') ? 'light' : 'dark',
                            })}
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

                <div className="flex-1 space-y-3 overflow-y-auto px-5 py-4 font-sans">
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
                                            ? 'border-border bg-surface text-text-muted'
                                            : 'border-success/40 bg-success-subtle text-text',
                                    )}
                                >
                                    <CheckCircle2 size={13} className={cn('shrink-0', gone ? 'text-text-faint' : 'text-success')} aria-hidden />
                                    <span className="truncate">{message.content}</span>
                                </div>
                            );
                        }
                        if (message.kind === 'human_prompt') {
                            return <HumanPromptCard key={message.key} content={message.content} />;
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
                                <div className="max-w-[88%] rounded-xl bg-surface px-4 py-3 text-sm text-text">
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
                    <div className="flex items-end gap-2">
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
                            className={cn(
                                'flex-1 resize-none rounded-lg border border-border-strong bg-surface px-3.5 py-2.5',
                                'font-sans text-sm text-text placeholder:text-text-faint',
                                'outline-none transition-colors focus:border-accent disabled:opacity-50',
                            )}
                        />
                        <button
                            type="button"
                            onClick={submit}
                            disabled={!input.trim() || streaming}
                            aria-label="Send"
                            className={cn(
                                'flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-accent text-accent-fg',
                                'transition-colors hover:bg-accent-hover disabled:cursor-not-allowed disabled:opacity-40',
                                focusRing,
                            )}
                        >
                            {streaming ? <Loader2 size={15} className="animate-spin" aria-hidden /> : <Send size={15} aria-hidden />}
                        </button>
                    </div>

                    <div className="flex items-center gap-1.5">
                        <span className="shrink-0 text-[10px] text-text-faint">Model</span>
                        <Combobox
                            value={sessionModel || '__default__'}
                            onChange={(v) => setSessionModel(v === '__default__' ? '' : v)}
                            aria-label="Model for this build"
                            searchPlaceholder="Search models…"
                            size="sm"
                            className="max-w-[200px]"
                            options={[
                                { value: '__default__', label: `Default (${builderModel || 'workspace model'})` },
                                ...models.map((m) => ({ value: m, label: m })),
                            ]}
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
                        <span className="ml-auto text-[10px] text-text-faint">Enter to send · Shift+Enter for a newline</span>
                    </div>
                </div>
            </aside>

            <HowImBuilt
                open={how !== null}
                onClose={() => setHow(null)}
                theme={how?.theme ?? 'dark'}
                onDuplicated={(id) => { window.location.href = `/orchestrations?id=${id}`; }}
            />
        </>
    );
}
