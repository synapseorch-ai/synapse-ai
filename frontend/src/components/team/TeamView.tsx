"use client";

/*
 * A Team agent's turn as a person reads it (Q5, Q5a): the task list at the top, the
 * exchanges between the lead and its members as one-line notes, then each member as a
 * line that opens — live with its time, Stop and Message; kept, as it ended (Naveen at
 * TEAM1b's screens).
 */
import React from 'react';
import { Check, Circle, X } from 'lucide-react';

import { cn } from '@/lib/cn';

import { TeamLanes, type LaneHandlers } from './TeamLanes';
import { hasTeam, taskCount, type TeamNote, type TeamState, type TeamTask } from './team';

function TaskMark({ task, needsYou }: { task: TeamTask; needsYou: boolean }) {
    if (task.status === 'done') return <Check aria-hidden className="size-3.5 shrink-0 text-success" />;
    if (task.status === 'failed') return <X aria-hidden className="size-3.5 shrink-0 text-danger" />;
    return <Circle aria-hidden className={cn('size-3 shrink-0', needsYou ? 'fill-person text-person' : 'text-machine')} />;
}

function taskWord(task: TeamTask, needsYou: boolean): { text: string; tone: string } {
    if (task.status === 'done') return { text: 'done', tone: 'text-text-muted' };
    if (task.status === 'failed') return { text: 'not done', tone: 'text-danger' };
    if (needsYou) return { text: 'needs you', tone: 'font-medium text-person-ink' };
    return { text: 'working', tone: 'text-accent-ink' };
}

/** The team's task list: each task, who has it, and how it stands. */
export function TeamTasks({ state, budget, className }: {
    state: TeamState;
    /** The Team agent's task budget, when the page knows it ("3 of 6 tasks"). */
    budget?: number | null;
    className?: string;
}) {
    if (!state.tasks.length) return null;
    const waiting = new Set(state.members.filter((m) => m.question && !m.question.answered).map((m) => m.id));
    return (
        <section aria-label="Task list" className={cn('min-w-0 rounded-xl border border-border bg-surface', className)}>
            <header className="flex items-baseline justify-between gap-3 border-b border-border px-3 py-2">
                <h3 className="text-xs font-medium text-text">Task list</h3>
                <span className="text-xs text-text-faint">{taskCount(state, budget)}</span>
            </header>
            <ol className="divide-y divide-border">
                {state.tasks.map((task) => {
                    const needsYou = waiting.has(task.memberId) && task.status === 'claimed';
                    const word = taskWord(task, needsYou);
                    return (
                        <li key={task.cardId || task.title} className="flex min-w-0 items-center gap-2.5 px-3 py-2 text-sm">
                            <TaskMark task={task} needsYou={needsYou} />
                            <span className="min-w-0 flex-1 truncate text-text" title={task.title}>{task.title}</span>
                            <span className="hidden shrink-0 text-xs text-text-muted sm:inline">{task.member}</span>
                            <span className={cn('shrink-0 text-xs', word.tone)}>{word.text}</span>
                        </li>
                    );
                })}
            </ol>
        </section>
    );
}

function noteLine(n: TeamNote): string {
    if (n.by === 'person') return `You to ${n.to}: ${n.text}`;
    if (n.by === 'lead') return `Lead to ${n.to}: ${n.text}`;
    return `${n.from} to ${n.to}: ${n.text}`;
}

/** The exchanges, one line each, in the order they happened. */
export function TeamNotes({ notes, className }: { notes: TeamNote[]; className?: string }) {
    if (!notes.length) return null;
    return (
        <ul aria-label="Team messages" className={cn('min-w-0 space-y-1 text-xs text-text-muted', className)}>
            {notes.map((n) => (
                <li key={n.id} className={cn('truncate', n.by === 'person' && 'text-text')} title={n.text}>
                    {noteLine(n)}
                </li>
            ))}
        </ul>
    );
}

/** The whole team: tasks, notes, lanes. Nothing when the lead answered alone. */
export function TeamView({ state, budget, kept = false, className, ...handlers }: LaneHandlers & {
    state: TeamState | null | undefined;
    budget?: number | null;
    /** A kept turn: nothing to stop or write to. */
    kept?: boolean;
    className?: string;
}) {
    if (!hasTeam(state)) return null;
    return (
        <div className={cn('flex min-w-0 flex-col gap-3', className)}>
            <TeamTasks state={state} budget={budget} />
            <TeamNotes notes={state.notes} />
            <TeamLanes members={state.members} {...(kept ? { onAnswer: handlers.onAnswer } : handlers)} />
        </div>
    );
}
