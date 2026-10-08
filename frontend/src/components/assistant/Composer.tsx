'use client';

/*
 * One composer for chat and the AI builder (M2c, UX1's "one composer").
 *
 * A rounded field holding, in one place, what the two screens kept in three: the
 * words, the agent (or "AI builder") as a pill, Attach, the model, and one round
 * button that is Send — or Stop while a turn streams, in the same spot, because
 * two buttons would mean one is always dead and the live one moves.
 *
 * Each product brings its own parts as slots: its picker (`pill`), its Attach
 * button and chips (`attach`, `top`; open source keeps images in the browser,
 * cloud uploads them), its model (`model`). The names of Send, Stop and the field
 * are props, so every screen keeps the accessible names its checks read.
 *
 * Keys: Enter sends, Shift+Enter is a new line, and a key that finishes an IME
 * composition never sends. The handler sits on the textarea alone — React events
 * bubble out of portals, so one on the form would send from a picker's search box.
 */
import { ArrowUp, Square } from 'lucide-react';
import * as React from 'react';

import { Spinner } from '@/components/ui';
import { cn } from '@/lib/cn';

export interface ComposerProps {
    value: string;
    onChange: (value: string) => void;
    /** Enter, or the Send button. The caller decides what sending means right now. */
    onSend: () => void;
    /** Whether there is something to send (words, or an image on its own). */
    canSend: boolean;
    /** Send's accessible name: "Send", "Resend", "Send your answer"… */
    sendLabel: string;
    /** A word on why Send is off, as its tooltip ("Still uploading an attachment…"). */
    sendTitle?: string;
    /** The run is starting and has no id to stop yet: Send shows the loader. */
    sendPending?: boolean;
    /** A turn is streaming: the button is Stop. */
    busy?: boolean;
    onStop?: () => void;
    /** Stop's accessible name and tooltip. */
    stopLabel: string;
    stopTitle?: string;
    placeholder: string;
    /** The field's name, as `aria-label`. */
    label?: string;
    /** Or a visually hidden `<label>` for the field (needs `id`). */
    labelText?: string;
    id?: string;
    disabled?: boolean;
    autoFocus?: boolean;
    onEscape?: () => void;
    textareaRef?: React.Ref<HTMLTextAreaElement>;
    /** Drag-and-drop onto the whole field. */
    dropHandlers?: Pick<React.HTMLAttributes<HTMLElement>, 'onDragEnter' | 'onDragOver' | 'onDragLeave' | 'onDrop'>;
    /** Pasting an image into the words. */
    pasteHandlers?: Pick<React.TextareaHTMLAttributes<HTMLTextAreaElement>, 'onPaste'>;
    /** Inside the field, above the words: attachment chips. */
    top?: React.ReactNode;
    /** The bar, left to right: the pill, Attach, then the model at the right before Send. */
    pill?: React.ReactNode;
    attach?: React.ReactNode;
    model?: React.ReactNode;
    /** The keys, under the field, until the conversation has a first message. */
    hint?: string;
    showHint?: boolean;
    /** Under the field, always: what must stay said ("Fixed for this conversation."). */
    note?: React.ReactNode;
    /** `page` for chat's full width, `panel` for the builder's card. */
    density?: 'page' | 'panel';
    /** Lines of room the empty field opens with. */
    rows?: number;
    className?: string;
}

const MAX_HEIGHT = 200;

export function Composer({
    value, onChange, onSend, canSend, sendLabel, sendTitle, sendPending = false, busy = false, onStop,
    stopLabel, stopTitle, placeholder, label, labelText, id, disabled = false, autoFocus = false, onEscape,
    textareaRef, dropHandlers, pasteHandlers, top, pill, attach, model, hint, showHint = true, note,
    density = 'page', rows = 2, className,
}: ComposerProps) {
    const inner = React.useRef<HTMLTextAreaElement | null>(null);
    // The caller's handle on the field (to focus it, say), without a second ref on the element.
    React.useImperativeHandle(textareaRef, () => inner.current as HTMLTextAreaElement, []);

    // The field grows with its lines up to MAX_HEIGHT, then scrolls; emptied, it
    // shrinks back. Measured after every change of the words, wherever they came
    // from — typing, a suggestion, `open({ prompt })`, a send that cleared them.
    React.useLayoutEffect(() => {
        const el = inner.current;
        if (!el) return;
        el.style.height = 'auto';
        el.style.height = `${Math.min(el.scrollHeight, MAX_HEIGHT)}px`;
    }, [value]);

    const page = density === 'page';
    const hasFooter = Boolean(hint) || Boolean(note);

    return (
        <div className={cn('w-full', className)}>
            <form
                onSubmit={(e) => {
                    e.preventDefault();
                    if (canSend) onSend();
                }}
                {...dropHandlers}
                className={cn(
                    'flex flex-col gap-1.5 border border-border-strong bg-surface shadow-elevated transition-[border-color,box-shadow]',
                    'focus-within:border-accent focus-within:ring-[3px] focus-within:ring-[var(--ring)]',
                    page ? 'rounded-2xl px-3 pb-2 pt-2.5' : 'rounded-xl px-2.5 pb-2 pt-2',
                )}
            >
                {top}
                {labelText && id && <label htmlFor={id} className="sr-only">{labelText}</label>}
                <textarea
                    ref={inner}
                    id={id}
                    value={value}
                    onChange={(e) => onChange(e.target.value)}
                    onKeyDown={(e) => {
                        if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
                            e.preventDefault();
                            if (canSend) onSend();
                            return;
                        }
                        if (e.key === 'Escape' && onEscape) onEscape();
                    }}
                    {...pasteHandlers}
                    placeholder={placeholder}
                    aria-label={labelText ? undefined : label}
                    rows={rows}
                    disabled={disabled}
                    autoFocus={autoFocus}
                    // The field around this owns the focus ring.
                    data-no-focus-ring
                    className={cn(
                        'w-full resize-none bg-transparent px-1 text-text outline-none placeholder:text-text-faint',
                        'disabled:cursor-not-allowed disabled:opacity-60',
                        page ? 'py-1 text-[15px] leading-relaxed' : 'py-0.5 text-sm leading-relaxed',
                    )}
                    style={{ maxHeight: MAX_HEIGHT }}
                />
                <div className="flex min-w-0 items-center gap-1">
                    {pill && <div className="flex min-w-0 shrink items-center">{pill}</div>}
                    {attach}
                    <span className="min-w-0 flex-1" />
                    {model && <div data-composer-model className="flex min-w-0 shrink items-center justify-end gap-1 text-xs text-text-faint">{model}</div>}
                    {busy ? (
                        <button
                            type="button"
                            onClick={onStop}
                            aria-label={stopLabel}
                            title={stopTitle ?? stopLabel}
                            className={cn(
                                'ml-1 flex size-8 shrink-0 items-center justify-center rounded-full border border-border-strong bg-surface text-text',
                                'transition-colors hover:bg-surface-2',
                            )}
                        >
                            <Square className="size-3 fill-current" aria-hidden />
                        </button>
                    ) : (
                        <button
                            type="submit"
                            disabled={!canSend || disabled || sendPending}
                            aria-label={sendLabel}
                            title={sendTitle}
                            className={cn(
                                'ml-1 flex size-8 shrink-0 items-center justify-center rounded-full bg-accent text-accent-fg',
                                'transition-colors hover:bg-accent-hover disabled:cursor-not-allowed disabled:opacity-40',
                            )}
                        >
                            {sendPending ? <Spinner size={16} /> : <ArrowUp className="size-4" aria-hidden />}
                        </button>
                    )}
                </div>
            </form>
            {hasFooter && (
                <div className={cn('flex min-h-5 items-center gap-3 px-2 pt-1.5 text-2xs text-text-faint', page ? '' : 'px-1')}>
                    {note && <span className="min-w-0 truncate">{note}</span>}
                    {hint && (
                        <span
                            aria-hidden={!showHint}
                            className={cn(
                                'ml-auto hidden shrink-0 transition-[opacity,visibility] duration-500 sm:inline',
                                showHint ? 'visible opacity-100' : 'invisible opacity-0',
                            )}
                        >
                            {hint}
                        </span>
                    )}
                </div>
            )}
        </div>
    );
}

/**
 * The look of the pill at the head of the composer's bar, for a product whose
 * picker is its own (open source's agent list) rather than the kit's
 * `Combobox trigger="pill"`. A static pill ("AI builder") uses it too.
 */
export const composerPillClass = cn(
    'inline-flex h-7 min-w-0 max-w-full items-center gap-1.5 whitespace-nowrap rounded-full bg-surface-2 pl-1 pr-2.5 text-xs font-medium text-text',
    'transition-colors hover:bg-row-hover',
);

/** The round tile at the head of a pill: the agent's icon, or the mark. */
export function PillAvatar({ children }: { children: React.ReactNode }) {
    return (
        <span aria-hidden className="flex size-5 shrink-0 items-center justify-center rounded-full bg-surface text-text-muted">
            {children}
        </span>
    );
}
