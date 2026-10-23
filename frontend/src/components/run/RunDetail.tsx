'use client';

/*
 * One run, as a path (UX1, M2b): the page both products draw a run with.
 *
 * Top to bottom:
 * - the header: the workflow, the run id, Open in editor, Cancel or Replay
 * - the product's notices (cloud: its place in line)
 * - the facts: status, when it started, wall clock, steps, tokens, cost
 * - why it failed, in one plain sentence and the fix; or the question it waits
 *   on, answered in place
 * - "Followed the saved workflow", or what differed
 * - the path: a waterfall of real starts and durations for a run that ran; the
 *   workflow's steps, as far as it got, for one that waits or stopped — drawn
 *   on the run's own copy of the workflow when it has one (F443), which is what
 *   it ran and carries on with, whatever was saved since
 * - what it produced, its plan, its activity and nested runs, its shared state,
 *   the raw events, and the product's own last sections (cloud's transcript,
 *   OSS's log)
 * - for a run whose step data retention removed (cloud), a line that says so,
 *   and "Removed" where its state and outputs were (F428)
 *
 * The data is handed in, mapped by each product (cloud renders on the server,
 * OSS in the browser); what it can do goes through `RunHost`. After an action
 * the page's `onChanged` reads the run again.
 */
import { CircleCheck, Info, OctagonAlert, UserRound } from 'lucide-react';
import Link from 'next/link';
import { useState, type ReactNode } from 'react';

import { Button, LinkButton, Mono, PageFrame, Section, StatusBadge, Toast, useToast } from '@/components/ui';
import { cn } from '@/lib/cn';
import type { Orchestration } from '@/types/orchestration';

import { ApprovalCard } from './ApprovalCard';
import { FailureCard } from './FailureCard';
import { RunResult } from './RunResult';
import { useRunHost } from './host';
import {
    bucketOf, CHARGE_RULE, chargeWords, creditsText, followedLine, money, pathOrder, REMOVED_NOTE, removedLine, runTotals, runWarnings,
    when, type Followed, type NestedRun, type RunRecord,
} from './model';
import { NestedRuns } from './NestedRuns';
import { RawEventLog } from './RawEventLog';
import { RunActivity } from './RunActivity';
import { RunOutputs } from './RunOutputs';
import { RunStepper } from './RunStepper';
import { RunTimeline } from './RunTimeline';
import { SharedState } from './SharedState';
import { TaskChecklist } from './TaskChecklist';
import { Took, useClock, When } from './When';

const FOLLOWED: Record<Followed['tone'], { cls: string; icon: typeof Info; iconCls: string }> = {
    success: { cls: 'bg-success-subtle', icon: CircleCheck, iconCls: 'text-success' },
    neutral: { cls: 'bg-surface-2', icon: Info, iconCls: 'text-text-muted' },
    person: { cls: 'bg-person-subtle', icon: UserRound, iconCls: 'text-person-ink' },
    danger: { cls: 'bg-danger-subtle', icon: OctagonAlert, iconCls: 'text-danger' },
};

function SectionHead({ title, aside }: { title: string; aside?: ReactNode }) {
    return (
        <div className="flex items-baseline justify-between gap-3">
            <h2 className="m-0 text-section text-text">{title}</h2>
            {aside && <span className="text-[12.5px] text-text-faint">{aside}</span>}
        </div>
    );
}

export function RunDetail({
    run, workflow, nested, onChanged, notices, extra, activityNote,
}: {
    run: RunRecord;
    /**
     * The workflow as it is saved today; `null` when it was deleted. The run is
     * drawn on its own copy (`run.definition`) when it has one, and this says
     * whether it was edited since.
     */
    workflow: Orchestration | null;
    /** The runs its steps started. */
    nested: NestedRun[];
    /** Read the run again (after an answer, a cancel, a replay). */
    onChanged: () => void;
    /** The product's notes above the facts (cloud: the run's place in line). */
    notices?: ReactNode;
    /** The product's last sections (cloud: Full transcript; OSS: Full log). */
    extra?: ReactNode;
    /** What Activity says when it has no events. */
    activityNote?: string;
}) {
    const host = useRunHost();
    const clock = useClock();
    const { toast, show, dismiss } = useToast();
    const [busy, setBusy] = useState(false);
    const bucket = bucketOf(run.status, run.waitingForHuman);
    const name = run.workflowName ?? 'Deleted workflow';
    const isNested = run.id.includes('__');

    async function act(action: () => Promise<string>) {
        setBusy(true);
        try {
            show(await action());
            onChanged();
        } catch (e) {
            show(e instanceof Error ? e.message : 'Something went wrong.', 'danger');
        } finally {
            setBusy(false);
        }
    }
    const cancel = () => act(() => host.cancel(run.id));
    const replay = () => act(() => host.replay(run.id));

    // What the run is drawn on: the steps it ran and carries on with (F443), or
    // today's for a run from before the copy was kept.
    const drawn = run.definition ?? workflow;
    // The cap per run it ran under (CAP1): its own copy's, never today's. A nested run ran under its root's.
    const capOf = run.id.includes('__') ? null : run.definition;
    const capCredits = capOf?.max_run_credits ?? null;
    const capUsd = capOf?.max_run_usd ?? null;
    const path = drawn ? pathOrder(drawn).filter((id) => drawn.steps.find((s) => s.id === id)?.type !== 'end') : [];
    const stepsLine = (bucket === 'completed' || !path.length)
        ? <><b className="font-semibold tabular-nums text-text">{run.history.length}</b> {run.history.length === 1 ? 'step' : 'steps'}</>
        : <><b className="font-semibold tabular-nums text-text">{new Set(run.history.filter((h) => h.status === 'completed').map((h) => h.step_id)).size}</b> of {path.length} steps done</>;
    const followed = followedLine(run, drawn, (iso) => when(iso, clock), run.definition ? workflow : undefined);
    const asking = bucket === 'needs' && run.status === 'paused' && Boolean(run.humanPrompt);
    const stopped = run.status === 'failed' || run.status === 'cancelled';
    const totals = runTotals(run);
    const asStepper = bucket === 'needs' || bucket === 'failed' || bucket === 'cancelled' || bucket === 'queued';
    const stateKeys = Object.keys(run.sharedState ?? {});
    const removed = removedLine(run, clock);
    // Steps that ended with their plan open (F862): "Completed with warnings", the status itself unchanged.
    const warnings = run.status === 'completed' ? runWarnings(run.history) : 0;
    const stepErrors = Object.fromEntries(run.history.filter((h) => h.error).map((h) => [h.step_id, String(h.error)]));

    const actions = (
        <div className="flex flex-wrap items-center gap-2">
            <LinkButton href={host.links.editor({ id: run.id, workflowId: run.workflowId })} size="sm">Open in editor</LinkButton>
            {/* A builder's (F452): anyone else got the API's 403. */}
            {host.canControl && (run.status === 'running' || run.status === 'queued') && (
                <Button variant="ghost" size="sm" className="text-danger hover:bg-danger-subtle hover:text-danger" disabled={busy} onClick={cancel}>
                    Cancel
                </Button>
            )}
            {host.canControl && stopped && !isNested && (
                <Button variant="secondary" size="sm" disabled={busy} onClick={replay}>Replay</Button>
            )}
        </div>
    );

    return (
        <PageFrame
            breadcrumb={
                <span className="flex min-w-0 items-center gap-1.5">
                    <Link href={host.links.runs} className="text-text-faint transition-colors hover:text-text">Runs</Link>
                    <span aria-hidden className="text-text-faint">/</span>
                    <span className="truncate text-text-faint">{name}</span>
                </span>
            }
            title={name}
            description={<Mono className="text-text-faint">{run.id}</Mono>}
            actions={actions}
            // Wide, like the Runs list: at 1440 the margins match on both sides (Naveen, M2b), and the
            // timeline and the log get the room.
            width="wide"
        >
            <div className="grid gap-7">
                {notices}

                {/* Each fact keeps to one line; the strip wraps between them, never inside one (F898). */}
                <div className="flex flex-wrap items-center gap-x-[22px] gap-y-1.5 text-[13.5px] text-text-muted [&>*]:whitespace-nowrap" aria-label="About this run">
                    <span className="inline-flex items-center gap-1.5">
                        <StatusBadge status={run.status} waitingForHuman={run.waitingForHuman} />
                        {warnings > 0 && (
                            <span className="text-sm font-medium text-text" title={`${warnings} ${warnings === 1 ? 'step' : 'steps'} ended with planned tasks still open: see the steps below`}>
                                with warnings
                            </span>
                        )}
                    </span>
                    {(run.startedAt || run.createdAt) && <When iso={run.startedAt || run.createdAt} clock={clock} />}
                    <span><b className="font-semibold tabular-nums text-text"><Took start={run.startedAt} end={run.endedAt} /></b> wall clock</span>
                    <span>{stepsLine}</span>
                    {/* Open, what its steps have spent so far: the run's own totals come at its end (F961). */}
                    <span><b className="font-semibold tabular-nums text-text">{totals.tokens == null ? '—' : totals.tokens.toLocaleString()}</b> tokens{totals.soFar ? ' so far' : ''}</span>
                    {run.creditsCharged != null && (
                        <span title={CHARGE_RULE}>
                            <b className="font-semibold tabular-nums text-text">{creditsText(run.creditsCharged)}</b> charged
                            {/* Which half of the rule it was (F869): "2 credits charged · 2 started minutes active". */}
                            {chargeWords(run) ? <span className="text-text-faint"> · {chargeWords(run)}</span> : null}
                            {capCredits ? <span className="text-text-faint"> · cap {creditsText(capCredits)}</span> : null}
                        </span>
                    )}
                    <span title={run.costHint}>
                        <b className="font-semibold tabular-nums text-text">{money(totals.costUsd)}</b> model cost{totals.soFar ? ' so far' : ''}
                        {capUsd ? <span className="text-text-faint"> · cap {money(capUsd)}</span> : null}
                    </span>
                </div>

                {/* The answer first (F901): the path and every output stay below it. */}
                {run.status === 'completed' && <RunResult run={run} workflow={drawn} />}
                {stopped && <FailureCard run={run} nested={nested} onReplay={replay} busy={busy} />}
                {asking && (
                    <ApprovalCard
                        run={run}
                        busy={busy}
                        onDone={(message) => { show(message); onChanged(); }}
                        onCancel={cancel}
                    />
                )}

                {removed && <FollowedLine followed={{ tone: 'neutral', text: removed, notes: [] }} />}
                {followed && <FollowedLine followed={followed} />}

                <section className="grid gap-2.5">
                    <SectionHead
                        title="The path"
                        aside={run.trigger ? <>Started by: {run.trigger}</> : undefined}
                    />
                    {asStepper
                        ? <RunStepper run={run} workflow={drawn} />
                        : <RunTimeline run={run} workflow={drawn} nested={nested} />}
                </section>

                {bucket !== 'queued' && (
                    <section className="grid gap-2.5">
                        <SectionHead title="What it produced" />
                        <RunOutputs run={run} workflow={drawn} />
                    </section>
                )}

                {run.tasks.length > 0 && (
                    <section className="grid gap-2.5">
                        <SectionHead title="Plan" aside="What the agent set out to do, and how far it got." />
                        <TaskChecklist tasks={run.tasks} />
                    </section>
                )}

                <section className="grid gap-3">
                    {/* Keyed on status: an answer, a cancel or a replay moves the run on, and both
                        sources replay from the first event — a fresh mount starts the log empty. */}
                    <RunActivity key={`${run.id}:${run.status}`} runId={run.id} status={run.status} stepErrors={stepErrors} emptyNote={activityNote} onChanged={onChanged} />
                    <NestedRuns runs={nested} />
                </section>

                <Section
                    title="Shared state"
                    description="The values steps read from and wrote to."
                    summary={removed && stateKeys.length === 0 ? 'Removed' : stateKeys.length === 1 ? '1 key' : `${stateKeys.length} keys`}
                >
                    {stateKeys.length === 0
                        ? <p className="text-sm text-text-muted">{removed ? REMOVED_NOTE : 'This run wrote no shared state.'}</p>
                        : <SharedState state={run.sharedState} />}
                </Section>

                <RawEventLog runId={run.id} />

                {extra}
            </div>
            <Toast message={toast.message} tone={toast.tone} onDismiss={dismiss} />
        </PageFrame>
    );
}

function FollowedLine({ followed }: { followed: Followed }) {
    const meta = FOLLOWED[followed.tone];
    const Icon = meta.icon;
    return (
        <p className={cn('m-0 inline-flex max-w-full items-start gap-2 justify-self-start rounded-[9px] px-3 py-2 text-[13px] text-text', meta.cls)}>
            <Icon className={cn('mt-0.5 size-4 shrink-0', meta.iconCls)} aria-hidden />
            <span>
                {followed.text}
                {followed.notes.map((n) => <span key={n} className="text-text-muted"> {n}</span>)}
            </span>
        </p>
    );
}
