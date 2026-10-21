'use client';
/* eslint-disable @typescript-eslint/no-explicit-any */
import { memo } from 'react';
import { Handle, Position } from '@xyflow/react';
import { AlertTriangle, Check, X } from 'lucide-react';
import { STEP_TYPE_META } from '@/types/orchestration';
import type { StepConfig, StepIssue, StepNodeData, StepType } from '@/types/orchestration';
import { cn } from '@/lib/cn';
import { reasonHeadline } from '@/lib/runErrors';
import { Spinner } from '@/components/ui';
import { StepIcon } from '../run/StepIcon';
import { creditsText, duration, money } from '../run/model';
import { roleOf } from './roles';
import { KIND_WORD, type AgentKind } from './agentKind';

/**
 * A connection's handle and its word. Every handle is the same quiet grey: the
 * word says which road it is (true, false, a route's name), and colour on the
 * canvas is kept for who does the work and for a run in progress (D211).
 */
const HANDLE_INK = 'var(--text-faint)';

/** Evenly distribute n source handles down the right edge. */
function handleOffsets(n: number): number[] {
    return Array.from({ length: n }, (_, i) => ((i + 1) / (n + 1)) * 100);
}

/** A right-edge source handle with its floating label, at `top` percent. */
function LabelledHandle({ id, top, label }: { id: string; top: number; label: string }) {
    return (
        <>
            <Handle
                type="source"
                position={Position.Right}
                id={id}
                className="!size-2.5 !border-2 !border-[var(--bg)]"
                style={{ top: `${top}%`, backgroundColor: HANDLE_INK }}
            />
            <span
                className="pointer-events-none absolute -right-2 translate-x-full -translate-y-1/2 rounded-sm bg-bg px-0.5 text-[10px] leading-none"
                style={{ top: `${top}%`, color: HANDLE_INK }}
                translate="no"
            >
                {label}
            </span>
        </>
    );
}

/** One line of per-type context in the card body. */
function summaryFor(step: StepConfig, agentName?: string, workflowName?: string): string | null {
    const clip = (s: string, n = 40) => (s.length > n ? s.slice(0, n) + '…' : s);
    switch (step.type) {
        case 'agent': return agentName ?? 'No agent selected';
        case 'workflow':
            if (!step.workflow_id) return 'No workflow selected';
            return workflowName ? `Run workflow: ${workflowName}` : 'Runs another workflow';
        case 'tool': return step.forced_tool ? (step.tool_arguments != null ? `${step.forced_tool} · set arguments` : step.forced_tool) : 'No tool selected';
        case 'llm': return step.prompt_template ? clip(step.prompt_template) : 'No prompt set';
        case 'evaluator': {
            const labels = Object.keys(step.route_map || {});
            return labels.length ? `${labels.length} route${labels.length !== 1 ? 's' : ''}: ${labels.join(', ')}` : 'No routes yet';
        }
        case 'parallel': {
            const n = (step.parallel_branches || []).length;
            return `${n} branch${n !== 1 ? 'es' : ''}`;
        }
        case 'merge': return step.merge_strategy || 'list';
        case 'loop':
            if (step.loop_over) return `for each ${clip(step.loop_over, 24)}`;
            if (step.loop_while_condition) return `while ${clip(step.loop_while_condition, 24)}`;
            return `${step.loop_count || 3}× iterations`;
        case 'human': return step.human_prompt ? clip(step.human_prompt) : 'Awaiting input…';
        case 'end': return 'Terminates flow';
        case 'extract_json':
            return step.input_keys?.length ? `From: ${step.input_keys.join(', ')}` : 'No input configured';
        case 'print': return step.print_content ? clip(step.print_content) : 'No content set';
        case 'if_else': return step.if_condition ? clip(step.if_condition) : 'No condition set';
        case 'switch': {
            const cases = Object.keys(step.switch_cases || {});
            return cases.length ? `${cases.length} case${cases.length !== 1 ? 's' : ''}: ${cases.join(', ')}` : 'No cases yet';
        }
        case 'transform': return step.transform_code ? 'Python transform' : 'No code set';
        case 'retrieve': {
            const n = step.retrieve?.kb_ids?.length ?? 0;
            if (!n) return 'No knowledge base';
            return step.retrieve?.query ? clip(step.retrieve.query) : `${n} knowledge base${n !== 1 ? 's' : ''}, no query`;
        }
        case 'ingest':
            if (!step.ingest?.kb_id) return 'No knowledge base';
            return step.ingest.records_from ? `From: ${step.ingest.records_from}` : 'No records key';
        default: return null;
    }
}

function StepNodeComponent({ data, selected }: { data: any; selected?: boolean }) {
    const step: StepConfig = data.step;
    const isEntry: boolean = data.isEntry;
    const runStatus: string | undefined = data.runStatus;
    const runError: string | undefined = runStatus === 'failed' ? data.runError : undefined;
    const agentName: string | undefined = data.agentName;
    const agentKindOf: string | undefined = step.type === 'agent' ? data.agentKind : undefined;
    const issues: StepIssue[] | undefined = data.issues;
    // The open canvas shows how the run stands in words on the step too (M3a-2).
    const runDetail = data.runDetail === true;
    const lastRun = data.lastRun as StepNodeData['lastRun'];
    const live = runStatus === 'running' || runStatus === 'paused';
    const meta = STEP_TYPE_META[step.type as StepType];
    if (!meta) return null;
    const role = roleOf(step.type);

    // Run state paints the frame; selection paints the ring. Both are tokens,
    // so the card is legible on either theme. A working step glows by who does
    // the work (D211): cyan for a model, a neutral ring for logic — a Parallel
    // or a Loop stays working through its whole body — and amber for a person.
    // A step waiting on a person also pulses (the overlay below), unless the
    // reader asked for less motion. Nothing on the frame itself animates.
    const statusFrame: Record<string, string> = {
        pending: 'border-border-strong',
        running: role === 'model' ? 'border-machine' : role === 'person' ? 'border-person' : 'border-text-muted',
        paused: 'border-person',
        completed: 'border-success',
        failed: 'border-danger',
    };
    // Kept apart from the frame so `cn` lets a status glow win over the resting shadow.
    const statusShadow: Record<string, string> = {
        running: role === 'model'
            ? 'shadow-[0_0_0_3px_color-mix(in_oklab,var(--machine)_24%,transparent),0_0_24px_-4px_var(--machine)]'
            : role === 'person'
                ? 'shadow-[0_0_0_3px_color-mix(in_oklab,var(--person)_30%,transparent)]'
                : 'shadow-[0_0_0_3px_color-mix(in_oklab,var(--text-muted)_22%,transparent)]',
        paused: 'shadow-[0_0_0_3px_color-mix(in_oklab,var(--person)_30%,transparent)]',
    };
    const frame = runStatus
        ? statusFrame[runStatus] || 'border-border-strong'
        : selected
            ? 'border-accent'
            : 'border-border-strong';

    const errorCount = (issues || []).filter((i) => i.severity === 'error').length;
    const warningCount = (issues || []).length - errorCount;

    const routeLabels = step.type === 'evaluator' ? Object.keys(step.route_map || {}) : [];
    const switchCaseLabels = step.type === 'switch' ? Object.keys(step.switch_cases || {}) : [];

    // Multi-handle nodes need vertical room for their fan-out.
    const handleCount =
        step.type === 'evaluator' ? routeLabels.length
        : step.type === 'switch' && switchCaseLabels.length > 0 ? switchCaseLabels.length + 1
        : step.type === 'loop' || step.type === 'if_else' ? 2
        : 1;
    const minHeight = handleCount > 2 ? handleCount * 24 + 48 : undefined;

    const summary = summaryFor(step, agentName, data.workflowName);

    return (
        // The status class comes first among the frame's `border-*` classes:
        // ui-smoke reads the first one as the step's state.
        <div
            className={cn(
                'relative min-w-[176px] max-w-[232px] rounded-xl border',
                frame,
                'bg-surface shadow-[0_1px_2px_rgb(0_0_0/0.06)] transition-[border-color,box-shadow,opacity]',
                data.unreached === true && 'opacity-55',
                runStatus && statusShadow[runStatus],
                selected && 'shadow-[0_0_0_3px_var(--ring)]',
            )}
            style={{ minHeight }}
        >
            {/* Waiting on a person: an amber ring that breathes, and holds still with reduced motion. */}
            {runStatus === 'paused' && (
                <span
                    aria-hidden
                    className="pointer-events-none absolute -inset-[6px] rounded-[16px] border-2 border-person/70 motion-safe:animate-pulse"
                />
            )}
            {/* Issue badge — errors take precedence over warnings */}
            {(errorCount > 0 || warningCount > 0) && (
                <span
                    className={`absolute -right-2 -top-2 z-10 flex size-[18px] items-center justify-center rounded-full border border-[var(--bg)] text-[10px] font-semibold ${
                        errorCount > 0 ? 'bg-danger text-danger-fg' : 'bg-warning text-bg'
                    }`}
                    title={(issues || []).map((i) => i.message).join('\n')}
                >
                    {errorCount > 0 ? errorCount : <AlertTriangle size={10} aria-hidden />}
                </span>
            )}

            {/* Input handle — left side */}
            <Handle type="target" position={Position.Left} className="!size-2.5 !border-2 !border-[var(--bg)] !bg-text-faint" />

            {/* Header: the type's icon on its role's tint (D211), the name, Start */}
            <div className="flex items-center gap-2 px-3 pb-1 pt-2.5">
                <StepIcon type={step.type} kind={agentKindOf} />
                <span className="min-w-0 flex-1 truncate text-[13px] font-semibold tracking-[-0.01em] text-text" translate="no">{step.name}</span>
                {isEntry && (
                    <span className="text-[10px] font-semibold uppercase tracking-[0.06em] text-accent-ink">
                        START
                    </span>
                )}
            </div>

            {/* Body */}
            <div className="space-y-0.5 px-3 pb-2.5">
                <div className="text-[10.5px] font-medium uppercase tracking-wider text-text-faint">
                    {/* An Agent step names what it holds (TEAM1e): "Agent · Team", "Agent · Workflow". */}
                    {agentKindOf && agentKindOf !== 'agent' ? `${meta.label} · ${KIND_WORD[agentKindOf as AgentKind]}` : meta.label}
                </div>

                {summary && (
                    <div className="truncate text-xs text-text-muted" translate="no">{summary}</div>
                )}
                {/* An agent step's own prompt template wins over the text typed
                    into the run box (context.py gives it precedence) — invisible
                    here is how a run "ignores" its input. Show it. */}
                {step.type === 'agent' && step.prompt_template && (
                    <div className="truncate text-[11px] text-text-faint" translate="no">
                        <span className="opacity-70">prompt:</span>{' '}
                        {step.prompt_template.slice(0, 40) + (step.prompt_template.length > 40 ? '…' : '')}
                    </div>
                )}
                {step.type === 'switch' && step.switch_expression && (
                    <div className="truncate font-code text-[11px] text-text-faint" translate="no">
                        {step.switch_expression.slice(0, 35) + (step.switch_expression.length > 35 ? '…' : '')}
                    </div>
                )}

                {/* I/O keys */}
                {step.input_keys && step.input_keys.length > 0 && (
                    <div className="truncate text-[11px] text-text-faint" translate="no">
                        <span className="opacity-70">in:</span> {step.input_keys.join(', ')}
                    </div>
                )}
                {step.output_key && (
                    <div className="truncate text-[11px] text-text-faint" translate="no">
                        <span className="opacity-70">out:</span> {step.output_key}
                    </div>
                )}
                {/* Why it failed, where the eye already is (F195). The run log
                    has the whole text; the card keeps it for a hover. */}
                {runError && (
                    <div className="line-clamp-2 break-words text-[11px] text-danger" title={runError} translate="no">
                        {reasonHeadline(runError)}
                    </div>
                )}
            </div>

            {/* Output handles — right side */}
            {step.type === 'end' ? null : step.type === 'evaluator' && routeLabels.length > 0 ? (
                handleOffsets(routeLabels.length).map((top, idx) => (
                    <LabelledHandle
                        key={`route_${routeLabels[idx]}`}
                        id={`route_${routeLabels[idx]}`}
                        top={top}
                        label={routeLabels[idx]}
                    />
                ))
            ) : step.type === 'loop' ? (
                <>
                    <LabelledHandle id="body" top={35} label="body" />
                    <LabelledHandle id="done" top={65} label="done" />
                </>
            ) : step.type === 'if_else' ? (
                <>
                    <LabelledHandle id="if_true" top={35} label="true" />
                    <LabelledHandle id="if_false" top={65} label="false" />
                </>
            ) : step.type === 'switch' && switchCaseLabels.length > 0 ? (
                handleOffsets(switchCaseLabels.length + 1).map((top, idx) =>
                    idx < switchCaseLabels.length ? (
                        <LabelledHandle
                            key={`case_${switchCaseLabels[idx]}`}
                            id={`case_${switchCaseLabels[idx]}`}
                            top={top}
                            label={switchCaseLabels[idx]}
                        />
                    ) : (
                        <LabelledHandle key="default" id="default" top={top} label="default" />
                    ),
                )
            ) : (
                <Handle type="source" position={Position.Right} className="!size-2.5 !border-2 !border-[var(--bg)] !bg-text-faint" />
            )}
            {/* How the run stands, on the open canvas: a pill on the bottom edge, placed
                over the border so the step's size — and its handles — never move. */}
            {runDetail && !live && lastRun && lastRun.state !== 'not-reached' && (
                <TimePill time={lastRun} />
            )}
            {runDetail && live && (
                <span
                    data-run-pill={runStatus}
                    className="pointer-events-none absolute -bottom-[9px] right-3 inline-flex h-[18px] items-center gap-1 rounded-full border border-border bg-surface px-1.5 text-[10.5px] font-medium leading-none text-text-muted"
                >
                    {runStatus === 'running' ? (
                        <>
                            <Spinner size={11} className={role === 'model' ? 'text-machine' : 'text-text-muted'} />
                            Working
                        </>
                    ) : (
                        <>
                            <span className="size-1.5 rounded-full bg-person" aria-hidden />
                            <span className="text-person-ink">Needs you</span>
                        </>
                    )}
                </span>
            )}

            {/* Where a route to this step goes at its Max iterations (F248), when it names one. */}
            {step.type !== 'end' && step.on_exhausted_step_id && (
                <Handle
                    type="source"
                    position={Position.Bottom}
                    id="limit"
                    title="When it reaches the limit"
                    className="!size-2.5 !border-2 !border-[var(--bg)]"
                    style={{ backgroundColor: HANDLE_INK }}
                />
            )}
        </div>
    );
}

/**
 * Last run on a step (M3a-2): how it ended and how long it took, with what it
 * spent where the run prices steps. Over the bottom border, like the live pill.
 */
function TimePill({ time }: { time: NonNullable<StepNodeData['lastRun']> }) {
    const took = time.seconds !== null ? duration(time.seconds) : null;
    const parts = [
        time.times ? `${time.times}×` : null,
        took,
        time.credits ? creditsText(time.credits) : null,
        time.costUsd !== undefined && time.costUsd !== null ? money(time.costUsd) : null,
    ].filter(Boolean).join(' · ');
    const lead = time.state === 'done'
        ? <Check className="size-3 text-success" aria-hidden />
        : time.state === 'failed'
            // The stroke, not the text colour: on a step, danger-inked text is its failure reason (F195).
            ? <X className="size-3 stroke-danger" aria-hidden />
            : time.state === 'waiting'
                ? <span className="size-1.5 rounded-full bg-person" aria-hidden />
                : null;
    const word = time.state === 'failed' ? 'Failed'
        : time.state === 'waiting' ? 'Waiting'
            : time.state === 'cancelled' ? 'Cancelled'
                : time.state === 'running' ? 'Running'
                    : 'Done';
    return (
        <span
            data-run-pill={time.state}
            data-step-seconds={time.seconds ?? undefined}
            title={`Last run: ${word.toLowerCase()}${parts ? `, ${parts}` : ''}`}
            className="pointer-events-none absolute -bottom-[9px] right-3 inline-flex h-[18px] items-center gap-1 rounded-full border border-border bg-surface px-1.5 text-[10.5px] font-medium tabular-nums leading-none text-text-muted"
        >
            {lead}
            {parts || word}
        </span>
    );
}

export const StepNode = memo(StepNodeComponent);
