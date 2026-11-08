"use client";

/*
 * A team's members (Q5, Q5a): one line each, the way a finished turn shows them, live too
 * (Naveen at TEAM1b's screens: a team may have more members than fit side by side). While
 * a member works its line says what it is doing and how long it has been at it, with Stop
 * beside it; opened, it shows its task, its calls, its answer as it writes it and Message.
 * A member that needs the person opens by itself, its question in its own lane.
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
import { duration, memberStatusLine, type MemberLaneData } from './team';

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

function isWorking(member: MemberLaneData): boolean {
    return member.status === 'running' || member.status === 'queued' || member.status === 'waiting_for_person';
}

/** How long the member has been at it, ticking while it works; nothing once it has ended (its line says). */
function useElapsed(member: MemberLaneData): string {
    const ticking = isWorking(member) && member.status !== 'queued' && member.startedAt !== undefined;
    const [now, setNow] = React.useState(() => Date.now());
    React.useEffect(() => {
        if (!ticking) return;
        const tick = window.setInterval(() => setNow(Date.now()), 1000);
        return () => window.clearInterval(tick);
    }, [ticking]);
    return ticking ? duration(Math.max(0, (now - (member.startedAt as number)) / 1000)) : '';
}

/** The line's status, coloured only for what it means: the machine at work, a person needed, how it ended. */
function StatusMark({ member, stopping, elapsed }: { member: MemberLaneData; stopping: boolean; elapsed: string }) {
    const needsYou = member.status === 'waiting_for_person' && !member.question?.answered;
    const working = isWorking(member) && !needsYou;
    const dot = needsYou ? 'bg-person' : working ? 'bg-machine' : member.status === 'completed' ? 'bg-success'
        : member.status === 'failed' ? 'bg-danger' : 'bg-text-faint';
    const text = needsYou ? 'font-medium text-person-ink' : working ? 'text-accent-ink'
        : member.status === 'failed' ? 'text-danger' : 'text-text-muted';
    const line = memberStatusLine(member, { stopping });
    return (
        <span className={cn('inline-flex min-w-0 items-center gap-1.5', text)} aria-live="polite">
            <span aria-hidden className={cn('size-2 shrink-0 rounded-full', dot, working && 'motion-safe:animate-pulse')} />
            <span className="truncate">{elapsed ? `${line} · ${elapsed}` : line}</span>
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
                        <p className={cn('mt-0.5 line-clamp-3 whitespace-pre-wrap break-words font-sans',
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
            <div className="flex min-w-0 flex-wrap items-center gap-2">
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

/** One member: a line that opens. It opens by itself when it needs the person. */
export function MemberLane({ member, className, ...handlers }: LaneHandlers & {
    member: MemberLaneData;
    className?: string;
}) {
    const working = isWorking(member);
    const needsYou = member.status === 'waiting_for_person' && !!member.question && !member.question.answered;
    const [open, setOpen] = React.useState(needsYou);
    React.useEffect(() => { if (needsYou) setOpen(true); }, [needsYou]);
    const stopping = handlers.stopping?.has(member.id) ?? false;
    const elapsed = useElapsed(member);
    const answer = member.answer || member.streaming || '';

    return (
        <section aria-label={`Member: ${member.name}`} className={cn('flex w-full min-w-0 flex-col items-start gap-1.5', className)}>
            <div className="flex min-w-0 max-w-full items-center gap-2">
                <button
                    type="button"
                    aria-expanded={open}
                    onClick={() => setOpen((v) => !v)}
                    className={cn('inline-flex min-w-0 max-w-full items-center gap-2 rounded-lg border bg-surface px-2.5 py-1.5 text-xs text-text-muted transition-colors hover:border-border-strong hover:text-text',
                        needsYou ? 'border-person/60' : 'border-border')}
                >
                    <Users aria-hidden className="size-3.5 shrink-0 text-machine" />
                    <span className="shrink-0 text-text">{member.name}</span>
                    <StatusMark member={member} stopping={stopping} elapsed={elapsed} />
                    <ChevronRight aria-hidden className={cn('size-3.5 shrink-0 text-text-faint transition-transform', open && 'rotate-90')} />
                </button>
                {working && handlers.onStop && (
                    <Button size="sm" variant="ghost" disabled={stopping} onClick={() => handlers.onStop?.(member)}
                            aria-label={`Stop ${member.name}`}>
                        <Square aria-hidden className="size-3" /> Stop
                    </Button>
                )}
            </div>

            {open && (
                <div className="flex w-full min-w-0 flex-col gap-2.5 border-l border-border pl-3 text-xs">
                    <div>
                        <p className="mb-1 font-medium text-text-muted">Its task</p>
                        <p className="whitespace-pre-wrap text-text">{member.task || '…'}</p>
                    </div>
                    <Calls member={member} all={!working} />
                    {member.helpers.length > 0 && <HelperLanes helpers={member.helpers} />}
                    {member.question && (
                        <MemberQuestion question={member.question} memberId={member.id} memberName={member.name}
                                        onAnswer={handlers.onAnswer} />
                    )}
                    {answer ? (
                        <div className="max-h-72 min-w-0 overflow-auto">
                            <p className="mb-1 font-medium text-text-muted">Its answer</p>
                            <Markdown className="text-sm leading-relaxed text-text">{answer}</Markdown>
                        </div>
                    ) : !working && member.error ? (
                        <p className="text-sm text-danger">Failed: {member.error}</p>
                    ) : null}
                    {!working && member.model && <p className="text-text-faint">Ran on {member.model}</p>}
                    {working && handlers.onMessage && member.status !== 'queued' && (
                        <MessageBox member={member} onMessage={handlers.onMessage} />
                    )}
                </div>
            )}
        </section>
    );
}

/** The members, one line each, in the order they started. */
export function TeamLanes({ members, className, ...handlers }: LaneHandlers & {
    members: MemberLaneData[];
    className?: string;
}) {
    if (!members.length) return null;
    return (
        <div className={cn('flex min-w-0 flex-col items-start gap-2', className)}>
            {members.map((m) => <MemberLane key={m.id} member={m} {...handlers} />)}
        </div>
    );
}
