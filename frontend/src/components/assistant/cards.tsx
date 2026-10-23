'use client';

/*
 * The AI builder's questions, as cards (M2c).
 *
 * The builder asks two kinds of question. Its plan — "here is what I'll build,
 * approve or tell me what to change" — becomes `PlanCard`: the steps numbered,
 * each coloured by who does the work (cyan a model, grey logic, amber a person),
 * with the answers as buttons. Every other question (what it needs to know
 * before it can plan) stays a `QuestionCard`, answered in the composer.
 *
 * The buttons answer the same question the composer does, through the same
 * request: Build it sends "approve", the word the question itself asks for.
 * Nothing about the builder changed for this; `plan.ts` reads its words. A
 * plan that names something missing here also offers Find who can, which sends
 * `RESEARCH_WORDS` and has the builder look for a provider instead (BWA1).
 *
 * A build drawn from its own object (SAI2-P3) offers what the object says: no
 * Build it while a tool has no way in, a "Build without …" for each such tool
 * instead, and Find who can only while nobody was looked up (Naveen, 10-23).
 */
import { AlertTriangle, Check, ChevronDown, ChevronUp, Search } from 'lucide-react';
import * as React from 'react';

import { EstimateLine, useRunEstimate, type EstimateLoader } from '@/components/orchestration/estimate';
import { Button, Markdown } from '@/components/ui';
import { cn } from '@/lib/cn';

import {
    changeOf, planDefinition, planFromChange, planFromPlanObject, planOf, readPlan, splitQuestion, withoutWords,
    type Plan, type PlanStep,
} from './plan';

/**
 * What a run of the plan would cost (CE1). The plan names every step once and
 * no branch or model, so its figure is a most, on the workspace's default model;
 * a plan with a loop says "or more".
 */
function PlanEstimate({ plan, load }: { plan: Plan; load: EstimateLoader }) {
    const definition = React.useMemo(() => planDefinition(plan), [plan]);
    const { estimate, busy, error } = useRunEstimate(load, definition);
    const loops = plan.steps.some((s) => s.type === 'loop');
    if (!definition || error) return null;
    const shown = estimate && loops && !estimate.open.includes('per_item')
        ? { ...estimate, open: [...estimate.open, 'per_item'] } : estimate;
    return <EstimateLine estimate={shown} busy={busy} compact ceiling />;
}

/** Past this many characters a question folds behind "Show all of it". */
export const QUESTION_FOLD = 600;

/** What the plan card's first button sends: the word the question asks for. */
export const APPROVE_WORD = 'approve';

/**
 * What "Find who can" sends when the plan names something missing here: the
 * builder looks for a provider instead of building (BWA1). Its branch matches
 * these exact words in code, so keep them in step with `step_review_research`.
 */
export const RESEARCH_WORDS = 'find who can';

function Header({ open, title }: { open: boolean; title?: string }) {
    return (
        <div className="flex items-center gap-2 text-xs">
            {open ? (
                <span className="inline-flex items-center gap-1.5 font-medium text-person-ink">
                    <span aria-hidden className="size-2 rounded-full bg-person" />
                    Needs your answer
                </span>
            ) : (
                <span className="font-medium text-text-faint">Asked</span>
            )}
            {title && <span className="text-text-faint">· {title}</span>}
        </div>
    );
}

function Fold({ expanded, onToggle }: { expanded: boolean; onToggle: () => void }) {
    return (
        <button
            type="button"
            onClick={onToggle}
            aria-expanded={expanded}
            className="inline-flex items-center gap-1 self-start rounded-md text-xs text-text-faint transition-colors hover:text-text"
        >
            {expanded
                ? <><ChevronUp className="size-3.5" aria-hidden /> Show less</>
                : <><ChevronDown className="size-3.5" aria-hidden /> Show all of it</>}
        </button>
    );
}

function Frame({ open, children, label }: { open: boolean; children: React.ReactNode; label: string }) {
    return (
        <section
            aria-label={label}
            className={cn(
                'flex min-w-0 flex-col gap-3 rounded-xl border bg-surface px-4 py-3.5',
                open ? 'border-person/60' : 'border-border',
            )}
        >
            {children}
        </section>
    );
}

/**
 * A question the run asked that is not a plan. Only the open one says it needs
 * an answer: a conversation brought back by a reload replays the questions
 * already answered (F214). `onStop` belongs on the open question — the composer
 * below is for the answer, so the other way out lives here.
 */
export function QuestionCard({ content, open, onStop }: { content: string; open: boolean; onStop?: () => void }) {
    const [expanded, setExpanded] = React.useState(false);
    const isLong = content.length > QUESTION_FOLD;
    const visible = isLong && !expanded ? `${content.slice(0, QUESTION_FOLD)}…` : content;
    return (
        <Frame open={open} label={open ? 'Needs your answer' : 'Asked'}>
            <Header open={open} />
            <Markdown className="text-sm leading-relaxed">{visible}</Markdown>
            {isLong && <Fold expanded={expanded} onToggle={() => setExpanded((v) => !v)} />}
            {open && onStop && (
                <div className="flex justify-end border-t border-border pt-2.5">
                    <Button type="button" variant="ghost" size="sm" onClick={onStop} className="text-danger hover:bg-danger-subtle hover:text-danger">
                        Stop instead
                    </Button>
                </div>
            )}
        </Frame>
    );
}

const NUMBER: Record<PlanStep['role'], string> = {
    model: 'bg-machine-subtle text-accent-ink',
    logic: 'bg-surface-2 text-text-muted',
    person: 'bg-person text-person-fg',
};

function Steps({ plan }: { plan: Plan }) {
    return (
        <ol aria-label={plan.kind === 'change' ? 'What it would do' : 'Steps'} className="flex flex-col">
            {plan.steps.map((step) => (
                <li
                    key={step.n}
                    className="grid grid-cols-[1.5rem_minmax(0,1fr)_auto] items-center gap-x-2.5 border-t border-border py-2 first:border-t-0 sm:py-2.5"
                >
                    <span
                        aria-hidden
                        className={cn(
                            'flex size-6 items-center justify-center rounded-full text-[11px] font-semibold tabular-nums',
                            step.deletes ? 'bg-danger-subtle text-danger' : NUMBER[step.role],
                        )}
                    >
                        {step.n}
                    </span>
                    <span className="min-w-0">
                        <span className={cn('block break-words text-sm', step.deletes ? 'font-medium text-danger' : 'text-text')}>
                            {step.deletes && <AlertTriangle className="mr-1 inline size-3.5 -translate-y-px" aria-hidden />}
                            {step.name}
                        </span>
                        {step.detail && <span className="block break-words text-xs text-text-muted">{step.detail}</span>}
                    </span>
                    {step.label ? (
                        <span className={cn('text-right text-xs', step.role === 'person' ? 'font-medium text-person-ink' : 'text-text-faint')}>
                            {step.label}
                            <span className="sr-only">{step.role === 'person' ? ', a person' : step.role === 'model' ? ', calls a model' : ''}</span>
                        </span>
                    ) : <span />}
                </li>
            ))}
        </ol>
    );
}

/**
 * The builder's plan, as a card. `prompt` is the question as asked; `kind` is
 * from `questionKind`. When no step can be read the card shows the words with
 * the same answers, so an unexpected plan still reads and can still be built.
 *
 * `onAnswer` sends through the panel's own answer path — what typing the word
 * and pressing Send does. `onRevise` takes the person to the composer.
 */
export function PlanCard({
    prompt, kind, open, onAnswer, onRevise, onStop, estimate, context,
}: {
    prompt: string;
    kind: 'build' | 'change';
    /** The question's typed object (F795): a change or a build draws from it, its words the fallback. */
    context?: Record<string, unknown> | null;
    open: boolean;
    onAnswer: (text: string) => void;
    onRevise: () => void;
    onStop?: () => void;
    /** The product's estimate route (CE1): a build plan says what a run of it would cost. */
    estimate?: EstimateLoader;
}) {
    const plan = React.useMemo(() => {
        const change = kind === 'change' ? changeOf(context) : null;
        const built = kind === 'build' ? planOf(context) : null;
        return (change && planFromChange(change, prompt)) ?? (built && planFromPlanObject(built, prompt))
            ?? readPlan(prompt, kind);
    }, [prompt, kind, context]);
    // What the card offers: the object's own list, or, for a plan read from its words, what it always offered.
    const offers = plan?.offers;
    const canBuild = !offers || offers.includes('build');
    const canFind = !offers || offers.includes('find');
    const without = (offers ?? []).filter((o) => o.startsWith('without:')).map((o) => o.slice('without:'.length));
    const [expanded, setExpanded] = React.useState(false);
    const [sent, setSent] = React.useState(false);
    const title = kind === 'change' ? 'The change' : 'The plan';
    const body = plan?.body ?? splitQuestion(prompt).body;
    const fallbackLong = !plan && body.length > QUESTION_FOLD;
    const fallbackText = fallbackLong && !expanded ? `${body.slice(0, QUESTION_FOLD)}…` : body;

    const facts = plan ? [
        plan.schedule,
        plan.newAgents.length
            ? `creates ${plan.newAgents.length === 1 ? 'an agent' : `${plan.newAgents.length} agents`}: ${plan.newAgents.join(', ')}`
            : null,
    ].filter(Boolean) as string[] : [];

    return (
        <Frame open={open} label={title}>
            <Header open={open} title={title} />
            {plan ? (
                <>
                    {plan.overview && <Markdown className="text-sm leading-relaxed text-text-muted">{plan.overview}</Markdown>}
                    <Steps plan={plan} />
                    {facts.length > 0 && (
                        <p className="text-xs text-text-muted">
                            {facts.map((f, i) => <React.Fragment key={f}>{i > 0 && ' · '}{i === 0 ? f.charAt(0).toUpperCase() + f.slice(1) : f}</React.Fragment>)}
                        </p>
                    )}
                    {estimate && <PlanEstimate plan={plan} load={estimate} />}
                    {plan.asksFirst && (
                        <div className="flex flex-col gap-1 rounded-lg bg-surface-2 px-3 py-2.5">
                            <span className="text-xs font-medium text-text">Asks you first</span>
                            <Markdown className="text-[13px] leading-relaxed">{plan.asksFirst}</Markdown>
                        </div>
                    )}
                    {plan.actsAlone && (
                        <div className="flex flex-col gap-1 rounded-lg bg-surface-2 px-3 py-2.5">
                            <span className="text-xs font-medium text-text">Acts without asking</span>
                            <Markdown className="text-[13px] leading-relaxed">{plan.actsAlone}</Markdown>
                        </div>
                    )}
                    {plan.needs && (
                        <div className="flex flex-col gap-1 rounded-lg border border-person/60 bg-person-subtle px-3 py-2.5">
                            <span className="inline-flex items-center gap-1.5 text-xs font-medium text-person-ink">
                                <span aria-hidden className="size-2 rounded-full bg-person" />
                                Missing here
                            </span>
                            <Markdown className="text-[13px] leading-relaxed">{plan.needs}</Markdown>
                            {open && (without.length > 0 || canFind) && (
                                <div className="mt-1 flex flex-wrap gap-2">
                                    {without.map((tool) => (
                                        <Button
                                            key={tool}
                                            type="button"
                                            variant="secondary"
                                            size="sm"
                                            disabled={sent}
                                            onClick={() => {
                                                setSent(true);
                                                onAnswer(withoutWords(tool));
                                            }}
                                        >
                                            Build without {tool}
                                        </Button>
                                    ))}
                                    {canFind && (
                                        <Button
                                            type="button"
                                            variant="secondary"
                                            size="sm"
                                            disabled={sent}
                                            onClick={() => {
                                                setSent(true);
                                                onAnswer(RESEARCH_WORDS);
                                            }}
                                        >
                                            <Search className="size-4" aria-hidden />
                                            Find who can
                                        </Button>
                                    )}
                                </div>
                            )}
                        </div>
                    )}
                    {plan.openQuestions && (
                        <div className="flex flex-col gap-1 rounded-lg bg-surface-2 px-3 py-2.5">
                            <span className="text-xs font-medium text-text">Open questions</span>
                            <Markdown className="text-[13px] leading-relaxed">{plan.openQuestions}</Markdown>
                        </div>
                    )}
                    {plan.notCarried && (
                        <div className="flex flex-col gap-1 rounded-lg bg-surface-2 px-3 py-2.5">
                            <span className="text-xs font-medium text-text">Not carried over</span>
                            <Markdown className="text-[13px] leading-relaxed">{plan.notCarried}</Markdown>
                        </div>
                    )}
                    {expanded && (
                        <div className="rounded-lg border border-border px-3 py-2.5">
                            <Markdown className="text-[13px] leading-relaxed">{prompt}</Markdown>
                        </div>
                    )}
                    <Fold expanded={expanded} onToggle={() => setExpanded((v) => !v)} />
                </>
            ) : (
                <>
                    <Markdown className="text-sm leading-relaxed">{fallbackText}</Markdown>
                    {fallbackLong && <Fold expanded={expanded} onToggle={() => setExpanded((v) => !v)} />}
                </>
            )}
            {open && (
                <div className="flex flex-wrap items-center gap-2 border-t border-border pt-3">
                    {onStop && (
                        <Button type="button" variant="ghost" size="sm" onClick={onStop} className="text-danger hover:bg-danger-subtle hover:text-danger">
                            Stop instead
                        </Button>
                    )}
                    {/* The two answers stay together, on the right, when the row wraps. */}
                    <div className="ml-auto flex flex-wrap justify-end gap-2">
                        <Button type="button" variant="secondary" size="sm" disabled={sent} onClick={onRevise}>
                            Change something
                        </Button>
                        {canBuild && (
                            <Button
                                type="button"
                                size="sm"
                                disabled={sent}
                                onClick={() => {
                                    // Once: a second press would answer a run that has already moved on.
                                    setSent(true);
                                    onAnswer(APPROVE_WORD);
                                }}
                            >
                                <Check className="size-4" aria-hidden />
                                {kind === 'change' ? 'Go ahead' : 'Build it'}
                            </Button>
                        )}
                    </div>
                </div>
            )}
        </Frame>
    );
}
