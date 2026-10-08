'use client';

/*
 * The conversation's pieces, shared by chat and the AI builder in both products
 * (M2c). Each product keeps its own turn reducer and its own words; these are
 * only the shapes, so the two chats and the two builders read as one thing:
 *
 *   - your message: a quiet bubble on the right
 *   - the answer: text beside an avatar, with no box around it; while a turn
 *     works, the avatar's slot holds the mark in the machine's colour
 *   - what ran on the way: quiet chips that open to their detail
 *   - suggestions under an empty composer: chips that fill it, unsent
 */
import { ChevronRight } from 'lucide-react';
import * as React from 'react';

import { Spinner } from '@/components/ui';
import { cn } from '@/lib/cn';

/** Your message. `actions` sit under it on the right (Edit); `attachments` above the words. */
export function UserBubble({
    children, actions, attachments, density = 'page', className, ...rest
}: React.HTMLAttributes<HTMLDivElement> & {
    actions?: React.ReactNode;
    attachments?: React.ReactNode;
    /** `panel` for the builder's narrower card: a size smaller. */
    density?: 'page' | 'panel';
}) {
    return (
        <div className={cn('group flex min-w-0 flex-col items-end gap-1', className)} {...rest}>
            {attachments}
            {/* The actions sit beside the bubble, on its left, so they take no line of their own. */}
            <div className="flex w-full min-w-0 items-start justify-end gap-1">
                {actions}
                <div className={cn(
                    'max-w-[85%] min-w-0 break-words rounded-2xl rounded-br-md bg-surface-2 px-3.5 py-2.5 leading-relaxed text-text sm:max-w-[78%]',
                    density === 'page' ? 'text-[15px]' : 'text-sm',
                )}>
                    {children}
                </div>
            </div>
        </div>
    );
}

/**
 * The agent's side of a turn: an avatar and, beside it, whatever the turn says.
 * `working` puts the mark in the avatar's slot at the avatar's size — one symbol
 * carrying the motion (feedback-app-shell-design, "chat's loading row").
 */
export function AgentTurn({
    avatar, working = false, size = 28, density = 'page', aside, children, className, ...rest
}: React.HTMLAttributes<HTMLDivElement> & {
    avatar?: React.ReactNode;
    working?: boolean;
    size?: number;
    density?: 'page' | 'panel';
    /** Under the avatar, in its column: the answer's own small actions (Copy), taking no line of their own. */
    aside?: React.ReactNode;
}) {
    return (
        <div className={cn('flex min-w-0 items-start gap-3', className)} {...rest}>
            <span className="flex shrink-0 flex-col items-center gap-1.5" style={{ width: size }}>
                <span className="flex items-center justify-center" style={{ width: size, height: size }}>
                    {working ? <Spinner size={size} className="text-machine" label="Working" /> : avatar}
                </span>
                {aside}
            </span>
            <div className={cn('flex min-w-0 flex-1 flex-col gap-2.5 leading-relaxed text-text', density === 'page' ? 'text-[15px]' : 'text-sm')}>{children}</div>
        </div>
    );
}

/** The agent's tile for `AgentTurn`'s avatar slot. */
export function AgentAvatar({ children, size = 28, className }: { children: React.ReactNode; size?: number; className?: string }) {
    return (
        <span
            className={cn('flex items-center justify-center rounded-lg bg-surface-2 text-text-muted', className)}
            style={{ width: size, height: size }}
        >
            {children}
        </span>
    );
}

/**
 * A quiet chip for something that ran: a tool, a hand-off, a step. With
 * `children` it opens to show them (the call's arguments, its result); without,
 * it is a plain line. `icon` takes the machine's colour.
 */
export function RunChip({
    icon, label, detail, defaultOpen = false, tone = 'default', className,
}: {
    icon?: React.ReactNode;
    /** The chip's words. */
    label: React.ReactNode;
    /** What opens under the chip. */
    detail?: React.ReactNode;
    defaultOpen?: boolean;
    tone?: 'default' | 'danger';
    className?: string;
}) {
    const [open, setOpen] = React.useState(defaultOpen);
    const body = (
        <>
            {icon && <span aria-hidden className={cn('flex shrink-0', tone === 'danger' ? 'text-danger' : 'text-machine')}>{icon}</span>}
            <span className="min-w-0 text-left">{label}</span>
        </>
    );
    const chip = 'inline-flex max-w-full items-center gap-2 rounded-lg border border-border bg-surface px-2.5 py-1.5 text-xs text-text-muted';
    if (!detail) return <div className={cn(chip, 'self-start', className)}>{body}</div>;
    return (
        <div className={cn('flex min-w-0 flex-col items-start gap-1.5', className)}>
            <button
                type="button"
                aria-expanded={open}
                onClick={() => setOpen((v) => !v)}
                className={cn(chip, 'transition-colors hover:border-border-strong hover:text-text')}
            >
                {body}
                <ChevronRight aria-hidden className={cn('size-3.5 shrink-0 text-text-faint transition-transform', open && 'rotate-90')} />
            </button>
            {open && <div className="w-full min-w-0">{detail}</div>}
        </div>
    );
}

/**
 * Suggestions under an empty composer. A tap fills the composer and sends
 * nothing — the person reads the words and presses Send, as with Home's
 * suggestions and `open({ prompt })`.
 */
export function Suggestions({
    items, onPick, label = 'Suggestions', className,
}: {
    items: readonly string[];
    onPick: (text: string) => void;
    label?: string;
    className?: string;
}) {
    if (!items.length) return null;
    return (
        <ul aria-label={label} className={cn('flex flex-wrap gap-2', className)}>
            {items.map((text) => (
                <li key={text} className="min-w-0 max-w-full">
                    <button
                        type="button"
                        onClick={() => onPick(text)}
                        className={cn(
                            'max-w-full rounded-2xl border border-border px-3 py-1.5 text-left text-[13px] leading-snug text-text-muted',
                            'transition-colors hover:border-border-strong hover:text-text',
                        )}
                    >
                        {text}
                    </button>
                </li>
            ))}
        </ul>
    );
}
