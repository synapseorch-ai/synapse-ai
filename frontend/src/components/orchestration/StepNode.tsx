'use client';
/* eslint-disable @typescript-eslint/no-explicit-any */
import { memo } from 'react';
import { Handle, Position } from '@xyflow/react';
import { AlertTriangle, Bot, Scale, GitBranch, GitMerge, RefreshCw, User, Code, Square, Zap, Wrench, Braces, GitFork, ArrowLeftRight, FileText } from 'lucide-react';
import { STEP_TYPE_META } from '@/types/orchestration';
import type { StepConfig, StepIssue, StepType } from '@/types/orchestration';
import { ROUTE_COLORS } from './graph';
import { reasonHeadline } from '@/lib/runErrors';

const ICONS: Record<string, React.FC<{ size?: number; className?: string }>> = {
    Bot, Scale, GitBranch, GitMerge, RefreshCw, User, Code, Square, Zap, Wrench, Braces, GitFork, ArrowLeftRight, FileText,
};

/** Evenly distribute n source handles down the right edge. */
function handleOffsets(n: number): number[] {
    return Array.from({ length: n }, (_, i) => ((i + 1) / (n + 1)) * 100);
}

/** A right-edge source handle with its floating label, at `top` percent. */
function LabelledHandle({ id, top, color, label }: { id: string; top: number; color: string; label: string }) {
    return (
        <>
            <Handle
                type="source"
                position={Position.Right}
                id={id}
                className="!size-2.5 !border-2 !border-[var(--bg)]"
                style={{ top: `${top}%`, backgroundColor: color }}
            />
            <span
                className="pointer-events-none absolute -right-2 translate-x-full -translate-y-1/2 text-[9px] leading-none"
                style={{ top: `${top}%`, color }}
                translate="no"
            >
                {label}
            </span>
        </>
    );
}

/** One line of per-type context in the card body. */
function summaryFor(step: StepConfig, agentName?: string): string | null {
    const clip = (s: string, n = 40) => (s.length > n ? s.slice(0, n) + '…' : s);
    switch (step.type) {
        case 'agent': return agentName ?? 'No agent selected';
        case 'tool': return step.forced_tool || 'No tool selected';
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
        default: return null;
    }
}

function StepNodeComponent({ data, selected }: { data: any; selected?: boolean }) {
    const step: StepConfig = data.step;
    const isEntry: boolean = data.isEntry;
    const runStatus: string | undefined = data.runStatus;
    const runError: string | undefined = runStatus === 'failed' ? data.runError : undefined;
    const agentName: string | undefined = data.agentName;
    const issues: StepIssue[] | undefined = data.issues;
    const meta = STEP_TYPE_META[step.type as StepType];
    if (!meta) return null;
    const IconComp = ICONS[meta.icon] || Bot;

    // Run state paints the frame; selection paints the ring. Both are tokens,
    // so the card is legible on either theme.
    const statusFrame: Record<string, string> = {
        pending: 'border-border-strong',
        running: 'border-accent shadow-[0_0_0_3px_var(--ring)] animate-pulse',
        paused: 'border-warning shadow-[0_0_12px_-2px_var(--warning)]',
        completed: 'border-success',
        failed: 'border-danger',
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

    const summary = summaryFor(step, agentName);

    return (
        <div
            className={`relative min-w-[170px] max-w-[230px] rounded-lg border-2 bg-surface shadow-sm transition-all ${frame} ${selected ? 'shadow-[0_0_0_3px_var(--ring)]' : ''}`}
            style={{ minHeight }}
        >
            {/* Issue badge — errors take precedence over warnings */}
            {(errorCount > 0 || warningCount > 0) && (
                <span
                    className={`absolute -right-2 -top-2 z-10 flex size-[18px] items-center justify-center rounded-full border border-[var(--bg)] text-[10px] font-semibold ${
                        errorCount > 0 ? 'bg-danger text-danger-fg' : 'bg-warning text-black'
                    }`}
                    title={(issues || []).map((i) => i.message).join('\n')}
                >
                    {errorCount > 0 ? errorCount : <AlertTriangle size={10} aria-hidden />}
                </span>
            )}

            {/* Input handle — left side */}
            <Handle type="target" position={Position.Left} className="!size-2.5 !border-2 !border-[var(--bg)] !bg-text-faint" />

            {/* Header */}
            <div className="flex items-center gap-2 rounded-t-[calc(var(--radius-lg)-2px)] border-b border-border px-3 py-2">
                <span
                    className="flex size-5 shrink-0 items-center justify-center rounded-md"
                    style={{ backgroundColor: meta.color + '26', color: meta.color }}
                    aria-hidden
                >
                    <IconComp size={12} />
                </span>
                <span className="min-w-0 flex-1 truncate text-xs font-medium text-text" translate="no">{step.name}</span>
                {isEntry && (
                    <span className="rounded-full bg-accent px-1.5 py-0.5 text-[9px] font-semibold tracking-wide text-accent-fg">
                        START
                    </span>
                )}
            </div>

            {/* Body */}
            <div className="space-y-1 px-3 py-2">
                <div className="text-[10px] uppercase tracking-wider text-text-faint">{meta.label}</div>

                {summary && (
                    <div className="truncate text-[11px] text-text-muted" translate="no">{summary}</div>
                )}
                {/* An agent step's own prompt template wins over the text typed
                    into the run box (context.py gives it precedence) — invisible
                    here is how a run "ignores" its input. Show it. */}
                {step.type === 'agent' && step.prompt_template && (
                    <div className="truncate text-[10px] text-text-faint" translate="no">
                        <span className="opacity-70">prompt:</span>{' '}
                        {step.prompt_template.slice(0, 40) + (step.prompt_template.length > 40 ? '…' : '')}
                    </div>
                )}
                {step.type === 'switch' && step.switch_expression && (
                    <div className="truncate font-code text-[10px] text-text-faint" translate="no">
                        {step.switch_expression.slice(0, 35) + (step.switch_expression.length > 35 ? '…' : '')}
                    </div>
                )}

                {/* I/O keys */}
                {step.input_keys && step.input_keys.length > 0 && (
                    <div className="truncate text-[10px] text-text-faint" translate="no">
                        <span className="opacity-70">in:</span> {step.input_keys.join(', ')}
                    </div>
                )}
                {step.output_key && (
                    <div className="truncate text-[10px] text-text-faint" translate="no">
                        <span className="opacity-70">out:</span> {step.output_key}
                    </div>
                )}
                {/* Why it failed, where the eye already is (F195). The run log
                    has the whole text; the card keeps it for a hover. */}
                {runError && (
                    <div className="line-clamp-2 break-words text-[10px] text-danger" title={runError} translate="no">
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
                        color={ROUTE_COLORS[idx % ROUTE_COLORS.length]}
                        label={routeLabels[idx]}
                    />
                ))
            ) : step.type === 'loop' ? (
                <>
                    <LabelledHandle id="body" top={35} color="var(--warning)" label="body" />
                    <LabelledHandle id="done" top={65} color="var(--success)" label="done" />
                </>
            ) : step.type === 'if_else' ? (
                <>
                    <LabelledHandle id="if_true" top={35} color="var(--success)" label="true" />
                    <LabelledHandle id="if_false" top={65} color="var(--danger)" label="false" />
                </>
            ) : step.type === 'switch' && switchCaseLabels.length > 0 ? (
                handleOffsets(switchCaseLabels.length + 1).map((top, idx) =>
                    idx < switchCaseLabels.length ? (
                        <LabelledHandle
                            key={`case_${switchCaseLabels[idx]}`}
                            id={`case_${switchCaseLabels[idx]}`}
                            top={top}
                            color={ROUTE_COLORS[idx % ROUTE_COLORS.length]}
                            label={switchCaseLabels[idx]}
                        />
                    ) : (
                        <LabelledHandle key="default" id="default" top={top} color="var(--flow-edge)" label="default" />
                    ),
                )
            ) : (
                <Handle type="source" position={Position.Right} className="!size-2.5 !border-2 !border-[var(--bg)] !bg-text-faint" />
            )}
            {/* Where a route to this step goes at its Max iterations (F248), when it names one. */}
            {step.type !== 'end' && step.on_exhausted_step_id && (
                <Handle
                    type="source"
                    position={Position.Bottom}
                    id="limit"
                    title="When it reaches the limit"
                    className="!size-2.5 !border-2 !border-[var(--bg)]"
                    style={{ backgroundColor: 'var(--warning)' }}
                />
            )}
        </div>
    );
}

export const StepNode = memo(StepNodeComponent);
