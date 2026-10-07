'use client';
/* eslint-disable @typescript-eslint/no-explicit-any */

/**
 * The step configuration panel — a resizable column docked to the canvas.
 *
 * Rebuilt on the design kit: what used to be 17 native `<select>`s, four
 * prop-drilled class-string constants and one 1,000-line conditional is now a
 * per-type registry (`panel/steps.tsx`), shared primitives (`panel/shared.tsx`)
 * and this shell, which owns the frame: identity, data flow, guardrails,
 * caching, and the issues readout from `validate.ts`.
 *
 * Prop contract note (D34): this component is forked verbatim into the cloud
 * repo, whose shell mounts it by name. Existing props keep their shapes; every
 * new prop is optional and degrades — without `orchestration` there are no
 * key suggestions or issue notes, without `showVaultHints={false}` the vault
 * hints show.
 */

import { useMemo, useRef, useState } from 'react';
import { AlertTriangle, X } from 'lucide-react';
import {
    Badge, Button, ConfirmDialog, Checkbox, ErrorNote, Field, IconButton, Input, Section, Select, TemplateScope, usePersisted,
} from '@/components/ui';
import type { Orchestration, StepConfig, StepType } from '@/types/orchestration';
import { KNOWLEDGE_STEP_TYPES, STEP_TYPE_META } from '@/types/orchestration';
import { collectStateKeys, hasTypeSpecificConfig, isLinearStep } from './graph';
import { validateOrchestration } from './validate';
import { KeyChips, StepTargetSelect, intOrUndefined, type KnowledgeBaseOption, type StepSectionProps } from './panel/shared';
import { STEP_SECTIONS } from './panel/steps';
import { useAvailableTools } from './panel/use-available-tools';
import { StepIcon } from '../run/StepIcon';

interface StepConfigPanelProps {
    step: StepConfig;
    agents: any[];
    allStepIds: { id: string; name: string }[];
    onUpdate: (step: StepConfig) => void;
    onDelete: () => void;
    onClose: () => void;
    isEntry: boolean;
    onSetEntry: () => void;
    availableModels?: string[];
    /** Enables key suggestions and per-step issue notes. Optional — see header note. */
    orchestration?: Orchestration;
    /** The cloud passes false: its panel has no vault-mention wiring. */
    showVaultHints?: boolean;
    /**
     * Whether this workspace may run customer code. Cloud passes
     * `plans.features.code_execution` (Team and Business); self-hosted leaves it
     * at true, where the runtime is the local Docker sandbox and there is no
     * plan to ask.
     *
     * A `transform` step runs Python, so it is gated exactly as a Python tool
     * is. It is greyed rather than removed on purpose: a capability nobody on
     * the entry tier can see is one nobody upgrades for — and a workflow copied
     * in from a Team org would otherwise carry a step type this picker cannot
     * even name.
     */
    canRunCode?: boolean;
    /**
     * One sentence on where a `transform` runs. The default is self-hosted's
     * answer; the cloud has no Docker and no host mode, and a note that names
     * both sends a person to a setting that does not exist.
     */
    codeRunnerNote?: string;
    /**
     * One sentence on what a step's semantic cache embeds with. Self-hosted's
     * default names the setting; its host says whether one is set. The cloud
     * embeds through its gateway, with nothing to set.
     */
    semanticCacheNote?: string;
    /**
     * Look, don't touch — for a person who may read the orchestration but not
     * change it (a cloud viewer). The fields show what is set and take no input;
     * sections still open, so nothing is hidden; there is no Delete step and no
     * "Set as entry point"; and `onUpdate` is never called, whatever reaches it.
     */
    readOnly?: boolean;
    /**
     * Whether Knowledge is on. Its Retrieve and Ingest types are offered only
     * then — though never taken from a step that already is one — and unset
     * reads as off, as on the canvas.
     */
    knowledgeEnabled?: boolean;
    /** The workspace's knowledge bases, for the Retrieve and Ingest pickers. Without them the ids are typed. */
    knowledgeBases?: KnowledgeBaseOption[];
    /**
     * `docked` (the default) is a column beside the canvas with its own width
     * and resize handle. `card` fills the floating setup card of the open canvas
     * (`CanvasWorkbench`, M3a), which owns the width; everything inside is the same.
     */
    frame?: 'docked' | 'card';
}

// The engine asks the runner for the internet (`Network.INTERNET` in the
// Transform executor), and a Transform in the Docker sandbox did open a socket
// when measured — this said "no network" (F70).
const SELF_HOSTED_RUNNER_NOTE =
    'Runs in Docker by default (512MB RAM, up to a minute, with internet access); switch to host mode in Settings → General to lift the sandbox.';

// F327: it embeds with the Embedding Model; with none set it never matches.
const SELF_HOSTED_SEMANTIC_NOTE =
    'It embeds each prompt with the Embedding Model in Settings → Models; with none set, only exact match runs.';

const STEP_TYPES: StepType[] = ['agent', 'llm', 'tool', 'evaluator', 'parallel', 'merge', 'loop', 'human', 'transform', 'extract_json', 'if_else', 'switch', 'print', 'end', 'retrieve', 'ingest'];

const MIN_WIDTH = 320;
const MAX_WIDTH = 640;

export function StepConfigPanel({
    step, agents, allStepIds, onUpdate, onDelete, onClose, isEntry, onSetEntry,
    availableModels, orchestration, showVaultHints = true, canRunCode = true,
    codeRunnerNote = SELF_HOSTED_RUNNER_NOTE, semanticCacheNote = SELF_HOSTED_SEMANTIC_NOTE, readOnly = false,
    knowledgeEnabled = false, knowledgeBases, frame = 'docked',
}: StepConfigPanelProps) {
    const update = (patch: Partial<StepConfig>) => {
        if (!readOnly) onUpdate({ ...step, ...patch });
    };
    const otherSteps = allStepIds.filter((s) => s.id !== step.id);
    const meta = STEP_TYPE_META[step.type];
    // An Agent step on a workflow agent runs that whole workflow, and waits on
    // the workflow's own timeout unless it is given one of its own (F291).
    const runsWorkflow = step.type === 'agent'
        && agents.find((a) => a.id === step.agent_id)?.type === 'orchestrator';

    // ── Resizable width, persisted ─────────────────────────────────────────
    const [persistedWidth, setPersistedWidth] = usePersisted<string>(
        'orch-panel-width',
        (raw) => {
            const n = parseInt(raw ?? '');
            return Number.isFinite(n) && n >= MIN_WIDTH && n <= MAX_WIDTH ? String(n) : '400';
        },
        '400',
    );
    const [width, setWidth] = useState(() => parseInt(persistedWidth));
    const widthRef = useRef(width);
    const startResize = (e: React.PointerEvent) => {
        e.preventDefault();
        const startX = e.clientX;
        const startW = widthRef.current;
        const onMove = (ev: PointerEvent) => {
            const next = Math.min(MAX_WIDTH, Math.max(MIN_WIDTH, startW + (startX - ev.clientX)));
            widthRef.current = next;
            setWidth(next);
        };
        const onUp = () => {
            window.removeEventListener('pointermove', onMove);
            setPersistedWidth(String(widthRef.current));
        };
        window.addEventListener('pointermove', onMove);
        window.addEventListener('pointerup', onUp, { once: true });
    };

    // ── Derived context ────────────────────────────────────────────────────
    const availableTools = useAvailableTools();
    const stateKeys = useMemo(
        () => (orchestration ? collectStateKeys(orchestration) : []),
        [orchestration],
    );
    const issues = useMemo(() => {
        if (!orchestration) return [];
        return validateOrchestration(orchestration, agents).byStep[step.id] || [];
    }, [orchestration, agents, step.id]);

    // ── Guarded type change ────────────────────────────────────────────────
    const [pendingType, setPendingType] = useState<StepType | null>(null);
    // Only an agent step reads Sees attachments, and the engine refuses it on any
    // other, so it goes with the type rather than being kept (F313).
    const toType = (next: StepType): Partial<StepConfig> =>
        next === 'agent' ? { type: next } : { type: next, pass_images: undefined };
    const changeType = (next: StepType) => {
        if (next === step.type) return;
        if (hasTypeSpecificConfig(step)) setPendingType(next);
        else update(toType(next));
    };

    const sectionProps: StepSectionProps = {
        step, update, otherSteps, agents,
        availableModels: availableModels || [],
        availableTools, stateKeys, showVaultHints, codeRunnerNote, orchestration, readOnly, knowledgeBases,
    };
    const TypeSection = STEP_SECTIONS[step.type];
    const isLinear = isLinearStep(step);

    return (
        <aside
            className={frame === 'card'
                ? 'relative flex h-full min-h-0 w-full flex-col'
                : 'relative flex shrink-0 flex-col border-l border-border bg-surface'}
            style={frame === 'card' ? undefined : { width }}
            aria-label="Step configuration"
        >
            {/* Resize handle (the card has its own, on its left edge) */}
            {frame !== 'card' && (
                <div
                    role="separator"
                    aria-orientation="vertical"
                    aria-label="Resize panel"
                    onPointerDown={startResize}
                    className="absolute inset-y-0 -left-0.5 z-10 w-1.5 cursor-col-resize transition-colors hover:bg-accent/40 active:bg-accent/60"
                />
            )}

            {/* Header: the type's icon on its role's tint (D211), the name, close */}
            <div className="flex items-center gap-2.5 border-b border-border px-4 py-3">
                <StepIcon type={step.type} className="size-7 [&_svg]:size-4" />
                <h3 className="min-w-0 flex-1 truncate text-[15px] font-semibold tracking-[-0.01em] text-text">{step.name}</h3>
                <IconButton label="Close panel" onClick={onClose} size="sm" icon={X} />
            </div>

            <div className="flex-1 space-y-4 overflow-y-auto p-4">
                {/* Issues for this step */}
                {issues.length > 0 && (
                    <div className="space-y-1.5">
                        {issues.filter((i) => i.severity === 'error').map((i, idx) => (
                            <ErrorNote key={idx} className="px-2.5 py-1.5 text-xs">{i.message}</ErrorNote>
                        ))}
                        {issues.filter((i) => i.severity === 'warning').map((i, idx) => (
                            <div key={idx} className="flex items-start gap-2 rounded-md border border-warning/25 bg-warning-subtle px-2.5 py-1.5 text-xs leading-relaxed text-warning">
                                <AlertTriangle className="mt-0.5 size-3.5 shrink-0" aria-hidden />
                                <span className="min-w-0">{i.message}</span>
                            </div>
                        ))}
                    </div>
                )}

                {readOnly && (
                    <p className="text-xs text-text-muted">View only — your role can read this step, not change it.</p>
                )}

                <Frozen readOnly={readOnly} className="space-y-4">
                    {/* Identity */}
                    <Field label="Name">
                        <Input size="sm" value={step.name} onChange={(e) => update({ name: e.target.value })} />
                    </Field>
                    <Field label="Type">
                        <Select
                            size="sm"
                            aria-label="Step type"
                            value={step.type}
                            onChange={(t) => changeType(t as StepType)}
                            options={STEP_TYPES.filter((t) => knowledgeEnabled || t === step.type
                                || !KNOWLEDGE_STEP_TYPES.includes(t)).map((t) => ({
                                value: t,
                                label: STEP_TYPE_META[t].label,
                                hint: t === 'transform' && !canRunCode
                                    ? 'Runs Python — available on Team and Business.'
                                    : STEP_TYPE_META[t].blurb,
                                // Never disable the type this step already is: a
                                // workflow that arrived with a transform must stay
                                // openable and editable, not become a step whose own
                                // type is unselectable in its own picker.
                                disabled: t === 'transform' && !canRunCode && step.type !== 'transform',
                            }))}
                        />
                    </Field>
                    {isEntry ? (
                        <Badge tone="success">Entry point</Badge>
                    ) : (
                        !readOnly && <Button size="sm" variant="ghost" onClick={onSetEntry}>Set as entry point</Button>
                    )}

                    {/* Type-specific behavior */}
                    <hr className="border-border" />
                    {/* The orchestration's state keys, for the prompts' `{{` suggestions. */}
                    <TemplateScope state={stateKeys}>
                        <TypeSection {...sectionProps} />
                    </TemplateScope>

                    {/* Data flow — not for end nodes */}
                    {step.type !== 'end' && (
                        <>
                            <hr className="border-border" />
                            <Field
                                label="Input keys"
                                hint={stateKeys.length > 0 ? 'Click a key to add it, or type a custom one.' : undefined}
                            >
                                <KeyChips
                                    value={step.input_keys || []}
                                    onChange={(input_keys) => update({ input_keys: input_keys.length ? input_keys : undefined })}
                                    suggestions={stateKeys}
                                />
                            </Field>
                            <Field label="Output key" hint="Where this step's result lands in shared state.">
                                <Input
                                    size="sm"
                                    className="font-code"
                                    value={step.output_key || ''}
                                    onChange={(e) => update({ output_key: e.target.value || undefined })}
                                    placeholder="analysis_result"
                                />
                            </Field>
                            {isLinear && (
                                <Field label="Next step">
                                    <StepTargetSelect
                                        aria-label="Next step"
                                        value={step.next_step_id}
                                        onChange={(id) => update({ next_step_id: id })}
                                        otherSteps={otherSteps}
                                    />
                                </Field>
                            )}
                            {step.type === 'loop' && (
                                <Field label="Done path" hint="After all iterations complete.">
                                    <StepTargetSelect
                                        aria-label="Done path"
                                        value={step.next_step_id}
                                        onChange={(id) => update({ next_step_id: id })}
                                        otherSteps={otherSteps}
                                    />
                                </Field>
                            )}
                        </>
                    )}
                </Frozen>

                {step.type !== 'end' && (
                    <>
                        {/* Guardrails — the section still opens for a viewer; its fields do not take input. */}
                        <Section
                            title="Guardrails"
                            summary={`${step.max_turns ?? '—'} turns · ${step.timeout_seconds ?? '—'}s`}
                            className="[&>button]:px-3 [&>button]:py-2.5 [&>div>div]:px-3"
                        >
                            <Frozen readOnly={readOnly} className="space-y-3">
                                <div className="grid grid-cols-2 gap-2">
                                    <Field label="Max turns">
                                        <Input size="sm" type="number" value={step.max_turns ?? ''} placeholder="15"
                                            onChange={(e) => update({ max_turns: intOrUndefined(e.target.value) })} />
                                    </Field>
                                    <Field
                                        label="Timeout (s)"
                                        hint={runsWorkflow ? '300 or blank: waits on the workflow\u2019s own timeout.' : undefined}
                                    >
                                        <Input size="sm" type="number" value={step.timeout_seconds ?? ''} placeholder="300"
                                            onChange={(e) => update({ timeout_seconds: intOrUndefined(e.target.value) })} />
                                    </Field>
                                </div>
                                <Field label="Max iterations" hint="Loop guard — how often this step may re-run.">
                                    <Input size="sm" type="number" value={step.max_iterations ?? ''} placeholder="3"
                                        onChange={(e) => update({ max_iterations: intOrUndefined(e.target.value) })} />
                                </Field>
                                <Field label="When it reaches the limit" hint="Where a route to this step goes once it has run Max iterations times.">
                                    <StepTargetSelect
                                        aria-label="When it reaches the limit"
                                        value={step.on_exhausted_step_id}
                                        onChange={(id) => update({ on_exhausted_step_id: id })}
                                        otherSteps={otherSteps}
                                        noneLabel="Fail the run"
                                    />
                                </Field>
                            </Frozen>
                        </Section>

                        <CacheSection step={step} update={update} readOnly={readOnly} semanticNote={semanticCacheNote} />
                    </>
                )}
            </div>

            {/* Footer */}
            {!readOnly && (
                <div className="border-t border-border p-3">
                    <Button size="sm" variant="ghost" className="w-full text-danger hover:text-danger" onClick={onDelete}>
                        Delete step
                    </Button>
                </div>
            )}

            <ConfirmDialog
                open={pendingType !== null}
                onClose={() => setPendingType(null)}
                onConfirm={() => {
                    if (pendingType) update(toType(pendingType));
                    setPendingType(null);
                }}
                title="Change step type?"
                description={`This step has ${meta.label}-specific configuration. It is kept but ignored by the new type — change back to see it again.${
                    step.pass_images && pendingType !== 'agent' ? ' Sees attachments is turned off: only agent steps read attachments.' : ''}`}
                confirmLabel="Change type"
            />
        </aside>
    );
}

/**
 * Cache controls: the response cache.
 *
 * Response cache is silently disabled for AGENT steps — skipping a ReAct loop
 * would let shared_state diverge from what the LLM "saw". There is no tool
 * cache: the engine's was removed (F67) and its toggle with it.
 */
function CacheSection({ step, update, readOnly = false, semanticNote }: {
    step: StepConfig; update: (patch: Partial<StepConfig>) => void; readOnly?: boolean; semanticNote: string;
}) {
    const responseCacheAllowed = step.type !== 'agent';
    const responseEnabled = !!step.cache_responses_enabled;
    const semanticEnabled = !!step.cache_semantic_enabled;

    const summary = responseEnabled ? 'responses on' : 'off';

    return (
        <Section title="Caching" summary={summary} className="[&>button]:px-3 [&>button]:py-2.5 [&>div>div]:px-3">
            <Frozen readOnly={readOnly} className="space-y-3">
                {responseCacheAllowed ? (
                    <div className="space-y-2">
                        <label className="flex cursor-pointer items-start gap-2">
                            <Checkbox
                                className="mt-0.5"
                                checked={responseEnabled}
                                onChange={(checked) => update({ cache_responses_enabled: checked || undefined })}
                                label="Cache LLM responses"
                            />
                            <span className="text-xs text-text">
                                Cache LLM responses
                                <span className="mt-0.5 block text-[10px] text-text-faint">
                                    Skip the call entirely when a previous run saw the same prompt. Cache hits cost ~0 (no tokens billed).
                                </span>
                            </span>
                        </label>

                        {responseEnabled && (
                            <>
                                <label className="flex cursor-pointer items-start gap-2 pl-6">
                                    <Checkbox
                                        className="mt-0.5"
                                        checked={semanticEnabled}
                                        onChange={(checked) => update({ cache_semantic_enabled: checked || undefined })}
                                        label="Semantic match"
                                    />
                                    <span className="text-xs text-text">
                                        Semantic match (fuzzy)
                                        <span className="mt-0.5 block text-[10px] text-text-faint">
                                            Reuse a near-identical prior response when exact match misses. {semanticNote}
                                        </span>
                                    </span>
                                </label>
                                <div className="grid grid-cols-2 gap-2 pl-6">
                                    <Field label="TTL (seconds)">
                                        <Input size="sm" type="number" value={step.cache_response_ttl_seconds ?? ''} placeholder="3600"
                                            onChange={(e) => update({ cache_response_ttl_seconds: intOrUndefined(e.target.value) })} />
                                    </Field>
                                    {semanticEnabled && (
                                        <Field label="Similarity ≥">
                                            <Input size="sm" type="number" min={0.5} max={1} step={0.01}
                                                value={step.cache_response_threshold ?? ''} placeholder="0.95"
                                                onChange={(e) => update({ cache_response_threshold: e.target.value === '' ? undefined : parseFloat(e.target.value) })} />
                                        </Field>
                                    )}
                                </div>
                            </>
                        )}
                    </div>
                ) : (
                    <p className="text-[11px] leading-relaxed text-text-faint">
                        Response cache is disabled for agent steps — skipping the ReAct loop would diverge shared state.
                    </p>
                )}
            </Frozen>
        </Section>
    );
}

/**
 * A disabled `<fieldset>` when read-only — every native control inside stops
 * taking input at once, Radix triggers included (they are buttons), while the
 * text stays readable. Kept inside a Section's body rather than around the
 * Section, so a viewer can still open it.
 */
function Frozen({ readOnly, className, children }: { readOnly: boolean; className?: string; children: React.ReactNode }) {
    return (
        // `mx-0`, not `m-0`: a fieldset's UA margin is horizontal only, and
        // `m-0` also cancelled the vertical gap its parent's `space-y-*` gives
        // it — which is why "Next step" sat 1px above the Guardrails card while
        // Guardrails and Caching had 16px between them.
        <fieldset disabled={readOnly} className={['mx-0 min-w-0 border-0 p-0', className].filter(Boolean).join(' ')}>
            {children}
        </fieldset>
    );
}
