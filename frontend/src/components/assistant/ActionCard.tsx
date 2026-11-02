'use client';

/*
 * The assistant's action card (ASSIST1, D291).
 *
 * The assistant never acts by itself. When a person asks it to run a workflow,
 * stop a run, turn a schedule off, or allow a tool call, it puts this card up
 * (`prepare_action`): what would happen, and a button per choice. Nothing has
 * happened yet. Pressing a button sends that button's token to the host's act
 * route through its `Actor` (each product's panel has one), which does exactly
 * that act as the person pressing, under their role, once.
 *
 * A run's question is answered on the card with the host's own answer form
 * (the run page's card), through the run's own door.
 */
import { ArrowRight, Check, CircleAlert } from 'lucide-react';
import Link from 'next/link';
import * as React from 'react';

import { Button, CopyButton } from '@/components/ui';
import { cn } from '@/lib/cn';
import type { ActionButton, ActionSpec } from '@/lib/builderEvents';

import { ChangeRows } from './ChangeRows';

/** What a host's act route answered. `spent`: the card cannot be pressed again (done before, expired, not theirs).
 *  `reply`: an agent's answer, for a card that asked it something (F1274). */
export type ActResult =
    | { ok: true; runId?: string | null; message?: string; publicKey?: string | null; reply?: string | null }
    | { ok: false; message: string; spent?: boolean };

export interface Actor {
    press: (button: ActionButton) => Promise<ActResult>;
    /** The host's answer form for a run's question; without one the card links to the run. */
    question?: (runId: string, onDone: (message: string) => void) => React.ReactNode;
    /** The host's path for a run, for "Open the run". */
    runHref: (runId: string) => string;
    /** The tag a site carries for a website widget's public key, on this host (F1262). */
    embedSnippet?: (publicKey: string) => string;
}

type Phase = { at: 'idle' } | { at: 'working'; label: string }
    | { at: 'done'; message: string; runId?: string | null; publicKey?: string | null; reply?: string | null }
    | { at: 'refused'; message: string; spent: boolean };

/** What a pressed button did, in the card's words. */
function doneLine(button: ActionButton, result: Extract<ActResult, { ok: true }>): string {
    if (result.message) return result.message;
    switch (button.tool) {
        case 'run_orchestration':
        case 'trigger_schedule':
            return 'Started.';
        case 'cancel_run':
            return 'Stopped.';
        case 'set_schedule_enabled':
            return button.arguments.enabled ? 'Turned on.' : 'Turned off.';
        case 'answer_tool_approval':
            return button.arguments.approve ? 'Allowed. The agent carries on.' : 'Refused. The agent was told.';
        case 'create_widget':
            return 'Made. Put this on each page it should show on:';
        case 'rotate_widget_key':
            return 'Done. The old snippet has stopped working; put this one in its place:';
        case 'chat_with_agent':
            return 'Its answer:';
        default:
            return 'Done.';
    }
}

export function ActionCard({ spec, actor }: { spec: ActionSpec; actor: Actor }) {
    const [phase, setPhase] = React.useState<Phase>({ at: 'idle' });
    const answered = phase.at === 'done';
    const locked = phase.at === 'working' || answered || (phase.at === 'refused' && phase.spent);

    async function press(button: ActionButton) {
        setPhase({ at: 'working', label: button.label });
        try {
            const result = await actor.press(button);
            if (result.ok) {
                setPhase({
                    at: 'done', message: doneLine(button, result), runId: result.runId, publicKey: result.publicKey,
                    reply: result.reply,
                });
            } else setPhase({ at: 'refused', message: result.message, spent: !!result.spent });
        } catch (exc) {
            setPhase({ at: 'refused', message: exc instanceof Error ? exc.message : 'That did not work. Try again.', spent: false });
        }
    }

    const runId = phase.at === 'done' ? phase.runId : null;
    const publicKey = phase.at === 'done' ? phase.publicKey : null;
    const snippet = publicKey && actor.embedSnippet ? actor.embedSnippet(publicKey) : null;
    const question = spec.question;
    // A run's question is the run page's own card, as Home's Needs you shows it: its fields, its refusals, its door.
    if (question && actor.question && phase.at !== 'done') {
        return <>{actor.question(question.run_id, (message) => setPhase({ at: 'done', message, runId: question.run_id }))}</>;
    }

    return (
        <section
            aria-label={spec.title}
            className={cn(
                'flex min-w-0 flex-col gap-3 rounded-xl border bg-surface px-4 py-3.5',
                answered ? 'border-border' : spec.danger ? 'border-danger/50' : 'border-person/60',
            )}
        >
            <div className="flex items-center gap-2 text-xs">
                {answered ? (
                    <span className="inline-flex items-center gap-1.5 font-medium text-success">
                        <Check className="size-3.5" aria-hidden />
                        Done
                    </span>
                ) : (
                    <span className={cn('inline-flex items-center gap-1.5 font-medium',
                        spec.danger ? 'text-danger' : 'text-person-ink')}>
                        <span aria-hidden className={cn('size-2 rounded-full', spec.danger ? 'bg-danger' : 'bg-person')} />
                        {question ? 'Waiting for your answer' : 'Needs your go-ahead'}
                    </span>
                )}
            </div>
            <div className="min-w-0">
                <h3 className="text-[15px] font-semibold leading-snug text-text [overflow-wrap:anywhere]">{spec.title}</h3>
                {spec.detail && (
                    <p className="mt-1 whitespace-pre-line text-[13px] leading-relaxed text-text-muted [overflow-wrap:anywhere]">{spec.detail}</p>
                )}
            </div>
            {spec.rows && spec.rows.length > 0 && <ChangeRows rows={spec.rows} />}

            {question && !answered && (
                <Link href={actor.runHref(question.run_id)} className="inline-flex items-center gap-1 text-[13px] font-medium text-accent hover:underline">
                    Answer on the run page <ArrowRight className="size-3.5" aria-hidden />
                </Link>
            )}

            {spec.buttons.length > 0 && !answered && (
                <div className="flex flex-wrap items-center gap-2 border-t border-border pt-3">
                    <span className="text-xs text-text-faint">Nothing happens until you press.</span>
                    {/* The choices stay together, on the right, the main one last, as on the plan card. */}
                    <div className="ml-auto flex flex-wrap justify-end gap-2">
                        {[...spec.buttons].reverse().map((button, i, all) => {
                            const main = i === all.length - 1;
                            return (
                                <Button
                                    key={`${button.label}-${i}`}
                                    type="button"
                                    size="sm"
                                    variant={main ? (spec.danger ? 'danger' : 'primary') : 'secondary'}
                                    disabled={locked}
                                    loading={phase.at === 'working' && phase.label === button.label}
                                    onClick={() => void press(button)}
                                >
                                    {main && !spec.danger && <Check className="size-4" aria-hidden />}
                                    {button.label}
                                </Button>
                            );
                        })}
                    </div>
                </div>
            )}

            {phase.at === 'done' && (
                <p className="flex flex-wrap items-center gap-x-2 text-[13px] text-text" role="status">
                    {phase.message}
                    {runId && (
                        <Link href={actor.runHref(runId)} className="inline-flex items-center gap-1 font-medium text-accent hover:underline">
                            Open the run <ArrowRight className="size-3.5" aria-hidden />
                        </Link>
                    )}
                </p>
            )}
            {phase.at === 'done' && phase.reply && (
                <div className="max-h-72 overflow-y-auto rounded-md border-l-2 border-border bg-surface-2 px-3 py-2 text-[13px] text-text [overflow-wrap:anywhere] whitespace-pre-wrap">
                    {phase.reply}
                </div>
            )}
            {snippet && (
                <div className="flex min-w-0 items-start gap-2">
                    <code className="min-w-0 flex-1 rounded-md bg-surface-2 px-2.5 py-2 font-mono text-xs text-text [overflow-wrap:anywhere]">
                        {snippet}
                    </code>
                    <CopyButton text={snippet} />
                </div>
            )}
            {phase.at === 'refused' && (
                <p className="flex items-start gap-1.5 text-[13px] text-danger" role="alert">
                    <CircleAlert className="mt-0.5 size-3.5 shrink-0" aria-hidden />
                    <span className="[overflow-wrap:anywhere]">{phase.message}</span>
                </p>
            )}
        </section>
    );
}
