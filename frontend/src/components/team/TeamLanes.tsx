"use client";

/*
 * A team's members, side by side (Q5a, artboard 3A; stacked on a phone). Each lane is one
 * member's task: what it is doing now, its calls, its answer as it writes it, a question
 * it needs the person for, and Message and Stop at its foot while it works. A kept turn
 * shows each as one line that opens to the same lane (Naveen, TEAM1b: readable history).
 *
 * Props and callbacks only (this folder's rule): the product says how to stop a member,
 * write to it and answer its question.
 */
import React from 'react';
import { ChevronRight, MessageSquare, Square, Users } from 'lucide-react';

import { cn } from '@/lib/cn';
import { Button, Markdown } from '@/components/ui';
import { HelperLanes } from '@/components/assistant/HelperLane';

import { MemberQuestion, type MemberAnswer } from './MemberQuestion';
import { memberStatusLine, type MemberLaneData } from './team';

export type LaneHandlers = {
    /** Stop one member (its lead reads that it was stopped; the rest work on). */
    onStop?: (member: MemberLaneData) => void;
    /** Write to one member; resolves to whether it was read (a member that just finished reads nothing). */
    onMessage?: (member: MemberLaneData, text: string) => Promise<boolean> | void;
    /** Answer a member's question. */
    onAnswer?: (answer: MemberAnswer) => void;
    /** Members whose Stop was pressed and who have not ended yet. */
    stopping?: Set<string>;
};

const RUNNING_CALLS = 4;

/** The lane's status dot and word, coloured only for what it means. */
function StatusMark({ member, stopping }: { member: MemberLaneData; stopping: boolean }) {
    const needsYou = member.status === 'waiting_for_person' && !member.question?.answered;
    const working = member.status === 'running' || (member.status === 'waiting_for_person' && !needsYou);
    const dot = needsYou ? 'bg-person' : working ? 'bg-machine' : member.status === 'completed' ? 'bg-success'
        : member.status === 'failed' ? 'bg-danger' : 'bg-text-faint';
    const text = needsYou ? 'font-medium text-person-ink' : working ? 'text-accent-ink'
        : member.status === 'failed' ? 'text-danger' : 'text-text-muted';
    return (
        <span className={cn('inline-flex min-w-0 items-center gap-1.5 text-xs', text)} aria-live="polite">
            <span aria-hidden className={cn('size-2 shrink-0 rounded-full', dot, working && 'motion-safe:animate-pulse')} />
            <span className="truncate">{memberStatusLine(member, { stopping })}</span>
        </span>
    );
}

function Calls({ member, all }: { member: MemberLaneData; all: boolean }) {
    const calls = all ? member.calls : member.calls.slice(-RUNNING_CALLS);
    const earlier = member.calls.length - calls.length;
    if (!calls.length) return null;
    return (
        <ol className="space-y-1 font-code text-xs" aria-label={`${member.name}'s calls`}>
            {earlier > 0 && <li className="text-text-faint">{earlier} earlier call{earlier === 1 ? '' : 's'}</li>}
            {calls.map((call, i) => (
                <li key={`${call.tool}-${earlier + i}`} className="min-w-0">
                    <p className="truncate text-text">
                        {call.tool}
                        {call.args && Object.keys(call.args).length > 0 && (
                            <span className="text-text-faint"> {JSON.stringify(call.args)}</span>
                        )}
                        <span className={cn('ml-1', call.refused ? 'text-danger' : call.result ? 'text-success' : 'text-text-faint')}>
                            {call.refused ? '· not run' : call.result ? '✓' : '…'}
                        </span>
                    </p>
                    {all && (call.result || call.refused) && (
                        <p className={cn('mt-0.5 whitespace-pre-wrap break-words font-sans',
                            call.refused ? 'text-danger' : 'text-text-muted')}>
                            {call.refused ? `Not run: ${call.reason || 'refused'}` : call.result}
                        </p>
                    )}
                </li>
            ))}
        </ol>
    );
}

function MessageBox({ member, onMessage }: { member: MemberLaneData; onMessage: NonNullable<LaneHandlers['onMessage']> }) {
    const [open, setOpen] = React.useState(false);
    const [text, setText] = React.useState('');
    const [said, setSaid] = React.useState<string | null>(null);
    const send = async () => {
        const words = text.trim();
        if (!words) return;
        const read = await onMessage(member, words);
        setSaid(read === false ? `${member.name} had already finished; not sent.` : 'Sent. It reads it before its next step.');
        setText('');
        setOpen(false);
    };
    if (!open) {
        return (
            <div className="flex min-w-0 flex-col gap-1">
                <Button size="sm" variant="ghost" onClick={() => { setOpen(true); setSaid(null); }}
                        aria-label={`Message ${member.name}`}>
                    <MessageSquare aria-hidden className="size-3.5" /> Message
                </Button>
                {said && <p className="text-xs text-text-faint" role="status">{said}</p>}
            </div>
        );
    }
    return (
        <form className="flex w-full min-w-0 gap-1.5" onSubmit={(e) => { e.preventDefault(); void send(); }}>
            <input
                autoFocus
                value={text}
                onChange={(e) => setText(e.target.value)}
                onKeyDown={(e) => { if (e.key === 'Escape') setOpen(false); }}
                aria-label={`Your message to ${member.name}`}
                placeholder={`To ${member.name}…`}
                maxLength={4000}
                className="min-w-0 flex-1 rounded-md border border-border-strong bg-surface px-2.5 py-1 text-sm text-text placeholder:text-text-faint"
            />
            <Button size="sm" type="submit" disabled={!text.trim()}>Send</Button>
        </form>
    );
}

/** One member's lane: full while the team works, or one line that opens to it (`compact`). */
export function MemberLane({
    member, compact = false, className, ...handlers
}: LaneHandlers & {
    member: MemberLaneData;
    compact?: boolean;
    className?: string;
}) {
    const working = member.status === 'running' || member.status === 'queued' || member.status === 'waiting_for_person';
    const [open, setOpen] = React.useState(false);
    const stopping = handlers.stopping?.has(member.id) ?? false;
    const finished = !working;
    const showWork = !compact || open;
    const answer = member.answer || member.streaming || '';

    if (compact && !open) {
        return (
            <section aria-label={`Member: ${member.name}`} className={cn('min-w-0', className)}>
                <button
                    type="button"
                    aria-expanded={false}
                    onClick={() => setOpen(true)}
                    className="inline-flex min-w-0 max-w-full items-center gap-2 rounded-lg border border-border bg-surface px-2.5 py-1.5 text-xs text-text-muted transition-colors hover:border-border-strong hover:text-text"
                >
                    <Users aria-hidden className="size-3.5 shrink-0 text-machine" />
                    <span className="shrink-0 text-text">{member.name}</span>
                    <span className="min-w-0 truncate">· {memberStatusLine(member)}</span>
                    <ChevronRight aria-hidden className="size-3.5 shrink-0 text-text-faint" />
                </button>
            </section>
        );
    }

    return (
        <section aria-label={`Member: ${member.name}`}
                 className={cn('flex min-w-0 flex-col rounded-xl border bg-surface',
                     member.status === 'waiting_for_person' && !member.question?.answered ? 'border-person/60' : 'border-border',
                     className)}>
            <header className="flex min-w-0 items-center gap-2 border-b border-border px-3 py-2">
                {compact ? (
                    <button type="button" aria-expanded onClick={() => setOpen(false)}
                            className="flex min-w-0 flex-1 items-center gap-2 text-left">
                        <ChevronRight aria-hidden className="size-3.5 shrink-0 rotate-90 text-text-faint" />
                        <span className="truncate text-sm font-medium text-text">{member.name}</span>
                    </button>
                ) : (
                    <span className="min-w-0 flex-1 truncate text-sm font-medium text-text">{member.name}</span>
                )}
                <StatusMark member={member} stopping={stopping} />
            </header>

            {showWork && (
                <div className="flex min-w-0 flex-1 flex-col gap-2.5 px-3 py-2.5">
                    <p className="line-clamp-2 text-xs text-text-muted" title={member.task}>{member.task}</p>
                    {(working || open) && <Calls member={member} all={finished} />}
                    {member.helpers.length > 0 && <HelperLanes helpers={member.helpers} />}
                    {member.question && (
                        <MemberQuestion question={member.question} memberId={member.id} memberName={member.name}
                                        onAnswer={handlers.onAnswer} />
                    )}
                    {answer ? (
                        <div className="max-h-72 min-w-0 overflow-auto text-sm text-text">
                            <Markdown className="text-sm leading-relaxed">{member.answer || member.streaming || ''}</Markdown>
                        </div>
                    ) : finished && member.error ? (
                        <p className="text-sm text-danger">Failed: {member.error}</p>
                    ) : null}
                    {finished && (
                        <div className="flex flex-wrap items-center gap-3 text-xs text-text-faint">
                            {!compact && member.calls.length > 0 && (
                                <button type="button" aria-expanded={open} onClick={() => setOpen((v) => !v)}
                                        className="text-accent-ink underline-offset-2 hover:underline">
                                    {open ? 'Hide its work' : 'View its work'}
                                </button>
                            )}
                            {member.model && <span>Ran on {member.model}</span>}
                        </div>
                    )}
                </div>
            )}

            {working && (handlers.onMessage || handlers.onStop) && (
                <footer className="flex min-w-0 flex-wrap items-start gap-1.5 border-t border-border px-2 py-1.5">
                    {handlers.onMessage && member.status !== 'queued' && (
                        <MessageBox member={member} onMessage={handlers.onMessage} />
                    )}
                    {handlers.onStop && (
                        <Button size="sm" variant="ghost" className="ml-auto" disabled={stopping}
                                onClick={() => handlers.onStop?.(member)} aria-label={`Stop ${member.name}`}>
                            <Square aria-hidden className="size-3" /> Stop
                        </Button>
                    )}
                </footer>
            )}
        </section>
    );
}

/** The lanes, in the order the members started: side by side where there is room, stacked on a phone. */
export function TeamLanes({ members, compact = false, className, ...handlers }: LaneHandlers & {
    members: MemberLaneData[];
    compact?: boolean;
    className?: string;
}) {
    if (!members.length) return null;
    if (compact) {
        return (
            <div className={cn('flex flex-col items-start gap-2', className)}>
                {members.map((m) => <MemberLane key={m.id} member={m} compact {...handlers} />)}
            </div>
        );
    }
    return (
        <div className={cn('grid min-w-0 items-start gap-3 [grid-template-columns:repeat(auto-fit,minmax(min(100%,16rem),1fr))]', className)}>
            {members.map((m) => <MemberLane key={m.id} member={m} {...handlers} />)}
        </div>
    );
}
