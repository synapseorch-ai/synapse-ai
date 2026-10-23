'use client';
/* eslint-disable @typescript-eslint/no-explicit-any */

/**
 * One section component per step type, in a registry keyed off `StepType` —
 * mirroring the backend's `STEP_EXECUTORS` so a new step type is one entry
 * here, not another branch in a 400-line conditional.
 */

import { useEffect, useState, type ReactNode } from 'react';
import { Plus, Trash2 } from 'lucide-react';
import { Button, Checkbox, CodeEditor, Combobox, Field, Input, MarkdownEditor, Select, TemplateInput, Textarea } from '@/components/ui';
import { vaultMentions } from '@/components/VaultMention';
import { humanizeToolName } from '@/lib/toolNames';
import type { IngestSettings, RetrieveSettings, StepConfig, StepType } from '@/types/orchestration';
import {
    KeyChips, LocalInput, ModelSelect, Note, StateKeyHelper, StepTargetSelect, intOrUndefined,
    type KnowledgeBaseOption, type StepSectionProps, type WorkflowOption,
} from './shared';
import { ExpressionVocabulary } from './ExpressionVocabulary';
import { PythonEditor } from './PythonEditor';
import { agentKind, agentOptions, memberNames, teamSettings, type KindAgent } from '../agentKind';
import { HUMAN_FIELD_TYPES, humanFieldOptions, humanFieldType } from '../humanFields';
import { argumentRows, withArgument, type ToolWithSchema } from '../toolArguments';
import { toolsRestricted } from '../allowedTools';

/* ── shared toggles ─────────────────────────────────────────────────────── */

/**
 * On re-invocation, show every prior turn's inputs/tools/output instead of just the last.
 *
 * Unset means on — the engine reads `None` as full history (`context.py`) — so
 * the box shows unset as ticked and writes `false`, the only value that turns
 * it off. It used to show unset as unticked and write `undefined` for
 * unticked: both states ran with full history (F76).
 */
function HistoryToggle({ step, update }: { step: StepConfig; update: (patch: Partial<StepConfig>) => void }) {
    return (
        <label className="flex cursor-pointer items-start gap-2 rounded-md border border-border bg-surface-2/40 px-3 py-2">
            <Checkbox
                className="mt-0.5"
                checked={step.include_full_history !== false}
                onChange={(checked) => update({ include_full_history: checked ? undefined : false })}
                label="Include full revision history"
            />
            <span className="text-xs text-text">
                Include full revision history
                <span className="mt-0.5 block text-[10px] text-text-faint">
                    On re-invocation (evaluator feedback or loop iteration), show every prior turn&apos;s
                    inputs, tools, and output — not just the last attempt. Increases prompt length.
                </span>
            </span>
        </label>
    );
}

/**
 * "Sees attachments": the images a person attached in chat reach this step (F313).
 *
 * Off unless ticked — the engine's `pass_images` defaults to false — and chat
 * offers Attach for a workflow agent only when some step has it on a model that
 * can read images. A step that runs another workflow passes them on to it, whose
 * own steps then choose.
 */
function SeesAttachmentsToggle({ step, update, runsWorkflow }: {
    step: StepConfig; update: (patch: Partial<StepConfig>) => void; runsWorkflow: boolean;
}) {
    return (
        <label className="flex cursor-pointer items-start gap-2 rounded-md border border-border bg-surface-2/40 px-3 py-2">
            <Checkbox
                className="mt-0.5"
                checked={!!step.pass_images}
                onChange={(checked) => update({ pass_images: checked || undefined })}
                label="Sees attachments"
            />
            <span className="text-xs text-text">
                Sees attachments
                <span className="mt-0.5 block text-[10px] text-text-faint">
                    {runsWorkflow
                        ? 'Pass the images a person attaches in chat on to this workflow; its own steps choose whether to read them.'
                        : 'The images a person attaches in chat reach this step. Use a model that can read images: chat offers Attach only when a step has this on.'}
                </span>
            </span>
        </label>
    );
}

/**
 * Narrow the agent's tool set for this step. `undefined` = agent default.
 *
 * The picker offers ONLY the selected agent's own tools — you can take access
 * away for a step, never add it. The engine enforces the same rule
 * (`narrow_allowed_tools` in `core/react_engine.py`), so a hand-written
 * definition cannot widen either; a saved selection the agent no longer has
 * is shown flagged rather than hidden, because the engine drops it at run
 * time and invisible dead weight in the definition helps nobody.
 */
function AllowedTools({ step, update, agents, availableTools }: StepSectionProps) {
    const agent = agents.find((a: any) => a.id === step.agent_id);
    // No agent yet means no set to narrow — the section appears once one is picked.
    if (!agent) return null;
    const agentTools: string[] = Array.isArray(agent.tools) ? agent.tools : ['all'];
    const offered = agentTools.includes('all') ? availableTools.map((t) => t.name) : agentTools;
    const descriptions = new Map(availableTools.map((t) => [t.name, t.description]));
    const selected = step.allowed_tools || [];
    const stale = selected.filter((t) => !offered.includes(t));
    const restricted = toolsRestricted(step);
    if (offered.length === 0 && stale.length === 0) return null;

    const toggle = (name: string, checked: boolean) => {
        update({
            allowed_tools: checked ? [...selected, name] : selected.filter((t) => t !== name),
        });
    };

    return (
        <div className="space-y-1.5 rounded-md border border-border bg-surface-2/40 px-3 py-2">
            <label className="flex cursor-pointer items-start gap-2">
                <Checkbox
                    className="mt-0.5"
                    checked={restricted}
                    onChange={(checked) => update({ allowed_tools: checked ? [] : undefined })}
                    label="Restrict tools for this step"
                />
                <span className="text-xs text-text">
                    Restrict tools for this step
                    <span className="mt-0.5 block text-[10px] text-text-faint">
                        Narrows this agent&apos;s own tool set — a step can remove access, never add it.
                    </span>
                </span>
            </label>
            {restricted && (
                <div className="max-h-40 space-y-1 overflow-y-auto pl-6 pt-1">
                    {offered.map((name) => (
                        <label key={name} className="flex cursor-pointer items-center gap-2">
                            <Checkbox
                                checked={selected.includes(name)}
                                onChange={(checked) => toggle(name, checked)}
                                label={name}
                            />
                            <span
                                className="truncate text-[11px] text-text-muted"
                                translate="no"
                                title={descriptions.get(name) ? `${name} — ${descriptions.get(name)}` : name}
                            >
                                {humanizeToolName(name)}
                            </span>
                        </label>
                    ))}
                    {stale.map((name) => (
                        <label key={name} className="flex cursor-pointer items-center gap-2">
                            <Checkbox
                                checked
                                onChange={() => toggle(name, false)}
                                label={`${name} (not in this agent's tools)`}
                            />
                            <span className="truncate text-[11px] text-warning" translate="no" title={`${name} — this agent no longer has this tool; the engine ignores it at run time. Uncheck to clean it up.`}>
                                {humanizeToolName(name)} — not in this agent&apos;s tools
                            </span>
                        </label>
                    ))}
                    {selected.length === 0 && (
                        <p className="text-[10px] text-warning">
                            Nothing selected yet — until you pick at least one tool, the agent keeps its own set.
                        </p>
                    )}
                </div>
            )}
        </div>
    );
}

/**
 * How an agent step finishes (E2): with its reply in words, as before, or by calling one of the tools it holds —
 * then its output is that call's result, an object, and a reply in words is reminded once and then fails the step.
 * Offered for an agent, not for a Team or a workflow, whose turns end their own way.
 */
function FinishTools({ step, update, agents, availableTools }: StepSectionProps) {
    const agent = agents.find((a: any) => a.id === step.agent_id);
    if (!agent || agentKind(agent) !== 'agent') return null;
    const agentTools: string[] = Array.isArray(agent.tools) ? agent.tools : ['all'];
    const offered = agentTools.includes('all') ? availableTools.map((t) => t.name) : agentTools;
    const chosen = step.finish_tools ?? [];
    const byTool = chosen.length > 0;
    return (
        <div className="space-y-2">
            <Field label="Finishes">
                <Select
                    size="sm"
                    aria-label="How the step finishes"
                    value={byTool ? 'tool' : 'reply'}
                    onChange={(v) => update(v === 'tool'
                        ? { finish_tools: offered[0] ? [offered[0]] : [] }
                        // What it ends with otherwise (F958) means nothing with no finish tools: the save
                        // would refuse it.
                        : { finish_tools: undefined, finish_fallback: undefined })}
                    options={[
                        { value: 'reply', label: 'With its reply', hint: 'The agent’s answer in words is the output' },
                        { value: 'tool', label: 'By calling a tool', hint: 'The tool’s result, an object, is the output' },
                    ]}
                />
            </Field>
            {byTool && (
                <div data-testid="finish-tools">
                    <KeyChips
                        value={chosen}
                        onChange={(names) => update(names.length ? { finish_tools: names }
                            : { finish_tools: undefined, finish_fallback: undefined })}
                        suggestions={offered}
                        inputLabel="Add a tool it finishes with"
                        placeholder="Add a tool by name…"
                    />
                    <p className="mt-1 text-[10px] text-text-faint">
                        The step ends when the agent calls one of these and the call succeeds. A reply in words is
                        reminded once, then the step fails.
                    </p>
                </div>
            )}
        </div>
    );
}

/* ── per-type sections ──────────────────────────────────────────────────── */

/**
 * The hint under an agent, LLM or tool step's prompt. These three render
 * `{{ state.key }}` and `{{ global.key }}` (and the older `{state.key}`), and
 * typing `{{` lists them — spaced, because an unspaced `{{state.key}}` renders
 * wrong in a step prompt.
 */
function PromptHint({ showVaultHints }: { showVaultHints?: boolean }) {
    return (
        <>
            Type <span className="font-code">{'{{'}</span> for state and variables —{' '}
            <span className="font-code">{'{{ state.key }}'}</span>, <span className="font-code">{'{{ global.key }}'}</span>
            {showVaultHints && <>. Type <span className="font-code text-accent">@</span> to reference a vault file</>}.
        </>
    );
}

/** One line of what goes with the step's kind: a label, and what it is. */
function KindLine({ label, children }: { label: string; children: ReactNode }) {
    return (
        <div className="flex gap-2">
            <dt className="w-16 shrink-0 text-text-faint">{label}</dt>
            <dd className="min-w-0 flex-1 text-text-muted">{children}</dd>
        </div>
    );
}

/**
 * What the Agent step holds, under its picker (TEAM1e; Naveen at its plan): a Team agent's members and how they
 * work, the step's time it works in and, in cloud, how it is priced; a workflow's own time limit. Read-only: a
 * Team is changed on its agent. Nothing for a plain agent.
 */
function AgentKindNote({ step, agents, tasksPriced }: Pick<StepSectionProps, 'step' | 'agents' | 'tasksPriced'>) {
    const agent = agents.find((a: KindAgent) => a.id === step.agent_id) as KindAgent | undefined;
    const kind = agentKind(agent);
    if (!agent || kind === 'agent') return null;
    const seconds = step.timeout_seconds ?? 300;
    if (kind === 'workflow') {
        return (
            <Note>
                Runs the workflow <span className="text-text">{agent.name}</span>{' '}inside this one step. A timeout
                of 300 or blank waits on that workflow&rsquo;s own limit.
            </Note>
        );
    }
    const names = memberNames(agent, agents);
    const team = teamSettings(agent);
    const asATeam = kind === 'team';
    const most = asATeam ? 1 + team.taskBudget : 1 + (step.max_turns ?? 15);
    return (
        <dl aria-label="The team in this step" className="space-y-1.5 rounded-md border border-border bg-surface-2/50 px-3 py-2 text-[11px] leading-relaxed">
            <KindLine label="Members">{names.length ? names.join(', ') : 'Any agent'}</KindLine>
            <KindLine label="Works">
                {asATeam
                    ? `As a team: ${team.atOnce} at once, ${team.taskBudget} tasks${team.spendLimit ? `, $${team.spendLimit} a turn` : ''}`
                    : 'One at a time: its lead hands work to one member and waits'}
            </KindLine>
            {asATeam && (
                <KindLine label="Time">
                    The team has this step&rsquo;s {seconds} s. Its lead is told what is left, and near the end no
                    more work is handed out.
                </KindLine>
            )}
            {tasksPriced && (
                <KindLine label="Price">1 credit, plus 1 for each member task — up to {most}.</KindLine>
            )}
            <p className="text-text-faint">Change the team on its agent.</p>
        </dl>
    );
}

function AgentSection(props: StepSectionProps) {
    const { step, update, agents, orchestration, showVaultHints } = props;
    return (
        <div className="space-y-3">
            <Field label="Agent" required>
                <Combobox
                    size="sm"
                    aria-label="Agent"
                    value={step.agent_id}
                    onChange={(id) => update({ agent_id: id || undefined })}
                    // Agents, Teams and Workflows, each in words (TEAM1e): never the stored type. Never this
                    // workflow's own agent (F853), as a Workflow step's picker never offers this workflow.
                    options={agentOptions(agents, orchestration?.id)}
                    placeholder="Select an agent, a team or a workflow…"
                    searchPlaceholder="Search agents…"
                />
            </Field>
            <AgentKindNote step={step} agents={agents} tasksPriced={props.tasksPriced} />
            <Field label="Prompt template" composite hint={<PromptHint showVaultHints={showVaultHints} />}>
                <MarkdownEditor
                    key={step.id}
                    compact
                    rows={4}
                    profile="step-prompt"
                    mentions={showVaultHints ? vaultMentions : undefined}
                    value={step.prompt_template || ''}
                    onChange={(prompt_template) => update({ prompt_template })}
                    placeholder="What this step should do. Type {{ for state and variables…"
                />
            </Field>
            <AllowedTools {...props} />
            <FinishTools {...props} />
            <HistoryToggle step={step} update={update} />
            <SeesAttachmentsToggle step={step} update={update}
                runsWorkflow={agents.find((a: any) => a.id === step.agent_id)?.type === 'orchestrator'} />
        </div>
    );
}

function LlmSection({ step, update, availableModels, showVaultHints }: StepSectionProps) {
    return (
        <div className="space-y-3">
            <Note>
                <strong>Single LLM call</strong> — no agent, no tools. Great for summaries, rewrites, and
                lightweight reasoning between steps.
            </Note>
            <Field label="Prompt template" required composite hint={<PromptHint showVaultHints={showVaultHints} />}>
                <MarkdownEditor
                    key={step.id}
                    compact
                    rows={5}
                    profile="step-prompt"
                    mentions={showVaultHints ? vaultMentions : undefined}
                    value={step.prompt_template || ''}
                    onChange={(prompt_template) => update({ prompt_template })}
                    placeholder={'Summarize the following in 3 bullet points:\n\n{{ state.analysis_result }}'}
                />
            </Field>
            <Field label="Model override">
                <ModelSelect value={step.model} onChange={(model) => update({ model })} availableModels={availableModels} />
            </Field>
            <HistoryToggle step={step} update={update} />
        </div>
    );
}

/**
 * A Tool step's arguments set on the step (E1): one row per argument the tool takes, then any other the step
 * already sets. Each value may read state with `{{ state.key }}`. Without the tool's schema (a host that does not list
 * it), a person adds arguments by name.
 */
function ToolArguments({ step, update, tool }: { step: StepConfig; update: StepSectionProps['update']; tool?: ToolWithSchema }) {
    const set = step.tool_arguments ?? {};
    const rows = argumentRows(tool, set);
    const [adding, setAdding] = useState('');
    const add = () => {
        const name = adding.trim();
        if (!name || name in set) return;
        update({ tool_arguments: { ...set, [name]: '' } });
        setAdding('');
    };
    return (
        <div className="space-y-2" data-testid="tool-arguments">
            {rows.length === 0 && (
                <p className="text-[11px] text-text-faint">
                    {step.forced_tool ? 'This tool takes no arguments.' : 'Pick a tool to set its arguments.'}
                </p>
            )}
            {rows.map((row) => (
                <div key={row.name} className="space-y-1">
                    <div className="flex items-center gap-2">
                        <span className="font-mono text-xs text-text">{row.name}</span>
                        {row.required && <span className="text-[10px] text-text-muted">required</span>}
                        {row.type && <span className="text-[10px] text-text-faint">{row.type}</span>}
                        {!row.declared && (
                            <button
                                type="button"
                                className="ml-auto text-text-faint hover:text-text"
                                aria-label={`Remove argument ${row.name}`}
                                onClick={() => {
                                    const next = { ...set };
                                    delete next[row.name];
                                    update({ tool_arguments: next });
                                }}
                            >
                                <Trash2 className="h-3.5 w-3.5" />
                            </button>
                        )}
                    </div>
                    <LocalInput
                        size="sm"
                        aria-label={`Argument ${row.name}`}
                        placeholder={row.description || '{{ state.key }} or a value'}
                        value={row.text}
                        onCommit={(text) => update({ tool_arguments: withArgument(set, row, text) })}
                    />
                </div>
            ))}
            <div className="flex items-center gap-2">
                <Input
                    size="sm"
                    aria-label="New argument name"
                    placeholder="Add an argument by name"
                    value={adding}
                    onChange={(e) => setAdding(e.target.value)}
                    onKeyDown={(e) => {
                        if (e.key === 'Enter') add();
                    }}
                />
                <Button size="sm" variant="secondary" onClick={add} disabled={!adding.trim()}>
                    <Plus className="h-3.5 w-3.5" /> Add
                </Button>
            </div>
            <p className="text-[10px] text-text-faint">
                Read a value from state with {'{{ state.key }}'}. Numbers, true / false and JSON are sent as values,
                except to an argument the tool says is text.
            </p>
        </div>
    );
}

function ToolSection({ step, update, availableModels, availableTools, showVaultHints }: StepSectionProps) {
    const selectedTool = availableTools.find((t) => t.name === step.forced_tool) as ToolWithSchema | undefined;
    const setHere = step.tool_arguments != null;
    return (
        <div className="space-y-3">
            <Note>
                {setHere ? (
                    <>
                        <strong>Tool call with set arguments</strong> — calls exactly one tool with the arguments
                        below, read from state. No model is asked, so it costs no model call.
                    </>
                ) : (
                    <>
                        <strong>Forced tool call</strong> — the LLM generates arguments for exactly one tool, then
                        calls it. If the first attempt fails, the ReAct loop retries up to <em>Max turns</em> times.
                    </>
                )}
            </Note>
            <Field label="Tool" required hint={selectedTool?.description}>
                <Combobox
                    size="sm"
                    aria-label="Tool"
                    value={step.forced_tool}
                    onChange={(name) => update({ forced_tool: name || undefined })}
                    options={availableTools.map((t) => ({ value: t.name, label: t.name, hint: t.description || undefined }))}
                    placeholder="Select tool…"
                    searchPlaceholder="Search tools…"
                />
            </Field>
            <Field label="Arguments">
                <Select
                    size="sm"
                    aria-label="Arguments"
                    value={setHere ? 'set' : 'ask'}
                    onChange={(v) => update({ tool_arguments: v === 'set' ? (step.tool_arguments ?? {}) : undefined })}
                    options={[
                        { value: 'ask', label: 'Ask the model', hint: 'A model writes them from the prompt' },
                        { value: 'set', label: 'Set here', hint: 'Fixed or read from state; no model call' },
                    ]}
                />
            </Field>
            {setHere ? (
                <ToolArguments step={step} update={update} tool={selectedTool} />
            ) : (
                <ToolPromptFields step={step} update={update} availableModels={availableModels} showVaultHints={showVaultHints} />
            )}
        </div>
    );
}

/** The prompt and model a Tool step's model reads to write its arguments (Arguments: Ask the model). */
function ToolPromptFields({
    step, update, availableModels, showVaultHints,
}: Pick<StepSectionProps, 'step' | 'update' | 'availableModels' | 'showVaultHints'>) {
    return (
        <>
            <Field label="Prompt template" composite hint={<PromptHint showVaultHints={showVaultHints} />}>
                <MarkdownEditor
                    key={step.id}
                    compact
                    rows={4}
                    profile="step-prompt"
                    mentions={showVaultHints ? vaultMentions : undefined}
                    value={step.prompt_template || ''}
                    onChange={(prompt_template) => update({ prompt_template })}
                    placeholder="Search for relevant data about {{ state.user_input }}"
                />
            </Field>
            <Field label="Model override">
                <ModelSelect value={step.model} onChange={(model) => update({ model })} availableModels={availableModels} />
            </Field>
            <HistoryToggle step={step} update={update} />
        </>
    );
}

/**
 * A Workflow step (E3): another workflow by its id, run as a nested run of this step. Its input is the rendered
 * prompt (the run's own input when it is empty); what it made is this step's output.
 */
function WorkflowSection({ step, update, orchestration, workflows, showVaultHints }: StepSectionProps) {
    const others: WorkflowOption[] = (workflows ?? []).filter((w) => w.id !== orchestration?.id);
    const picked = others.find((w) => w.id === step.workflow_id);
    return (
        <div className="space-y-3">
            <Note>
                <strong>Run another workflow</strong>{' '}— runs it from the start as part of this run, waits for it
                to finish, and writes what it made to this step&apos;s output. A question it asks is answered here.
            </Note>
            <Field label="Workflow" required hint={picked?.description || undefined}>
                {workflows ? (
                    <Combobox
                        size="sm"
                        aria-label="Workflow"
                        value={step.workflow_id}
                        onChange={(id) => update({ workflow_id: id || undefined })}
                        options={others.map((w) => ({ value: w.id, label: w.name || w.id, hint: w.description || undefined }))}
                        placeholder="Select a workflow…"
                        searchPlaceholder="Search workflows…"
                    />
                ) : (
                    <LocalInput
                        size="sm"
                        aria-label="Workflow id"
                        placeholder="orch_…"
                        value={step.workflow_id || ''}
                        onCommit={(id) => update({ workflow_id: id.trim() || undefined })}
                    />
                )}
            </Field>
            <Field label="Its input" composite hint={<PromptHint showVaultHints={showVaultHints} />}>
                <MarkdownEditor
                    key={step.id}
                    compact
                    rows={3}
                    profile="step-prompt"
                    mentions={showVaultHints ? vaultMentions : undefined}
                    value={step.prompt_template || ''}
                    onChange={(prompt_template) => update({ prompt_template: prompt_template || undefined })}
                    placeholder="Empty: this run's own input. Or e.g. Summarise {{ state.notes }}"
                />
            </Field>
        </div>
    );
}

function EvaluatorSection({ step, update, otherSteps, availableModels }: StepSectionProps) {
    return (
        <div className="space-y-3">
            <Field label="Evaluator prompt" hint="Instructions for the routing decision.">
                <Textarea
                    size="sm"
                    rows={3}
                    value={step.evaluator_prompt || ''}
                    onChange={(e) => update({ evaluator_prompt: e.target.value })}
                    placeholder="e.g. If login is needed, route to human…"
                />
            </Field>
            <Field label="Model override">
                <ModelSelect value={step.model} onChange={(model) => update({ model })} availableModels={availableModels} />
            </Field>
            <div>
                <span className="mb-1.5 block text-sm font-medium text-text">
                    Routes <span className="text-xs font-normal text-text-faint">(LLM picks one)</span>
                </span>
                <div className="space-y-2">
                    {Object.entries(step.route_map || {}).map(([label, targetId]) => (
                        <div key={label} className="space-y-1.5 rounded-md border border-border bg-surface-2/40 p-2">
                            <LocalInput
                                value={label}
                                aria-label="Route label"
                                placeholder="Label"
                                onCommit={(newLabel) => {
                                    if (newLabel === label || !newLabel.trim()) return;
                                    const rename = (obj: Record<string, any> | undefined) =>
                                        obj && Object.fromEntries(Object.entries(obj).map(([k, v]) => [k === label ? newLabel : k, v]));
                                    update({
                                        route_map: rename(step.route_map) as Record<string, string | null>,
                                        route_descriptions: rename(step.route_descriptions) as Record<string, string>,
                                    });
                                }}
                            />
                            <StepTargetSelect
                                aria-label={`Target for route ${label}`}
                                value={targetId}
                                noneLabel="End workflow"
                                onChange={(target) =>
                                    update({ route_map: { ...(step.route_map || {}), [label]: target ?? null } })
                                }
                                otherSteps={otherSteps}
                            />
                            <LocalInput
                                value={(step.route_descriptions || {})[label] || ''}
                                aria-label={`Description for route ${label}`}
                                placeholder="When should this route be chosen? (helps the LLM decide)"
                                onCommit={(desc) =>
                                    update({ route_descriptions: { ...(step.route_descriptions || {}), [label]: desc } })
                                }
                            />
                            <Button
                                size="sm"
                                variant="ghost"
                                className="w-full text-danger hover:text-danger"
                                onClick={() => {
                                    const newMap = { ...(step.route_map || {}) };
                                    delete newMap[label];
                                    const newDescs = { ...(step.route_descriptions || {}) };
                                    delete newDescs[label];
                                    update({ route_map: newMap, route_descriptions: newDescs });
                                }}
                            >
                                <Trash2 size={12} aria-hidden /> Remove route
                            </Button>
                        </div>
                    ))}
                    <Button
                        size="sm"
                        variant="secondary"
                        onClick={() => {
                            const existing = Object.keys(step.route_map || {});
                            update({ route_map: { ...(step.route_map || {}), [`route_${existing.length + 1}`]: null } });
                        }}
                    >
                        <Plus size={12} aria-hidden /> Add route
                    </Button>
                </div>
            </div>
        </div>
    );
}

function ParallelSection({ step, update, otherSteps }: StepSectionProps) {
    return (
        <div className="space-y-2">
            <span className="block text-sm font-medium text-text">Branches</span>
            {(step.parallel_branches || []).map((branch, branchIdx) => (
                <div key={branchIdx} className="flex items-center gap-2">
                    <span className="w-6 shrink-0 text-[10px] font-semibold text-text-faint">B{branchIdx + 1}</span>
                    <div className="flex-1">
                        <StepTargetSelect
                            aria-label={`Branch ${branchIdx + 1} entry step`}
                            value={branch[0]}
                            noneLabel="Select entry step…"
                            onChange={(id) => {
                                const newBranches = [...(step.parallel_branches || [])];
                                newBranches[branchIdx] = id ? [id] : [];
                                update({ parallel_branches: newBranches });
                            }}
                            otherSteps={otherSteps}
                        />
                    </div>
                    <button
                        type="button"
                        aria-label={`Remove branch ${branchIdx + 1}`}
                        onClick={() =>
                            update({ parallel_branches: (step.parallel_branches || []).filter((_, i) => i !== branchIdx) })
                        }
                        className="text-text-faint transition-colors hover:text-danger"
                    >
                        <Trash2 size={12} aria-hidden />
                    </button>
                </div>
            ))}
            <Button
                size="sm"
                variant="secondary"
                onClick={() => update({ parallel_branches: [...(step.parallel_branches || []), []] })}
            >
                <Plus size={12} aria-hidden /> Add branch
            </Button>
            <p className="text-[10px] text-text-faint">
                Each branch auto-follows its entry step&apos;s Next-step chain. Connect steps with edges on the canvas.
            </p>
        </div>
    );
}

function MergeSection({ step, update }: StepSectionProps) {
    return (
        <Field label="Merge strategy">
            <Select
                size="sm"
                aria-label="Merge strategy"
                value={step.merge_strategy || 'list'}
                onChange={(v) => update({ merge_strategy: v as StepConfig['merge_strategy'] })}
                options={[
                    { value: 'list', label: 'List', hint: 'Array of branch outputs' },
                    { value: 'concat', label: 'Concat', hint: 'Text join' },
                    { value: 'dict', label: 'Dict', hint: 'Keyed by source step' },
                ]}
            />
        </Field>
    );
}

type LoopMode = 'count' | 'foreach' | 'while';

function loopMode(step: StepConfig): LoopMode {
    if (step.loop_over) return 'foreach';
    if (step.loop_while_condition) return 'while';
    return 'count';
}

function LoopSection({ step, update, otherSteps }: StepSectionProps) {
    // The mode a person picked for this step, until the fields say it anyway.
    // Read from the fields alone, "while" with no condition yet *is* "count" —
    // so picking it snapped the picker straight back and its condition field
    // never appeared (Naveen, 2026-09-19). Held per step id, so opening another
    // step reads that step's fields. Nothing new is stored: a while loop saved
    // with no condition runs as a count, and reopens as one.
    const [picked, setPicked] = useState<{ stepId: string; mode: LoopMode } | null>(null);
    const mode = picked?.stepId === step.id ? picked.mode : loopMode(step);
    // Switching mode clears the other modes' fields, so the engine's precedence
    // (collection, then condition, then count) can never be a surprise.
    const setMode = (next: LoopMode) => {
        if (next === mode) return;
        setPicked({ stepId: step.id, mode: next });
        update({
            loop_over: next === 'foreach' ? (step.loop_over || 'state.items') : undefined,
            loop_while_condition: next === 'while' ? (step.loop_while_condition || '') : undefined,
            loop_count: next === 'count' ? (step.loop_count ?? 3) : step.loop_count,
        });
    };

    return (
        <div className="space-y-3">
            <Field label="Repeat">
                <Select
                    size="sm"
                    aria-label="Loop mode"
                    value={mode}
                    onChange={(v) => setMode(v as LoopMode)}
                    options={[
                        { value: 'count', label: 'A fixed number of times', hint: 'Runs the body N times' },
                        { value: 'foreach', label: 'Once per item', hint: 'Iterates a list from state' },
                        { value: 'while', label: 'While a condition holds', hint: 'Re-checked each iteration' },
                    ]}
                />
            </Field>

            {mode === 'count' && (
                <Field label="Loop count">
                    <Input
                        size="sm"
                        type="number"
                        min={1}
                        value={step.loop_count ?? 3}
                        onChange={(e) => update({ loop_count: parseInt(e.target.value) || 3 })}
                    />
                </Field>
            )}

            {mode === 'foreach' && (
                <>
                    <Field label="Collection" hint="An expression over state, e.g. state.items">
                        <LocalInput
                            size="sm"
                            aria-label="Collection expression"
                            placeholder="state.items"
                            value={step.loop_over || ''}
                            onCommit={(v) => update({ loop_over: v })}
                        />
                    </Field>
                    <Field label="Item key" hint="Each item lands here">
                        <LocalInput
                            size="sm"
                            aria-label="Item key"
                            placeholder="loop_item"
                            value={step.loop_item_key || ''}
                            onCommit={(v) => update({ loop_item_key: v || undefined })}
                        />
                    </Field>
                </>
            )}

            {mode === 'while' && (
                <Field label="While condition" hint="Checked before each iteration, e.g. (state.n or 0) < 5">
                    <LocalInput
                        size="sm"
                        aria-label="While condition"
                        placeholder="(state.n or 0) < 5"
                        value={step.loop_while_condition || ''}
                        onCommit={(v) => update({ loop_while_condition: v })}
                    />
                </Field>
            )}

            {/* Every mode writes it (F212): a question in the body can say which
                iteration is asking. */}
            <Field label="Index key" hint="0-based position, written before each iteration">
                <LocalInput
                    size="sm"
                    aria-label="Index key"
                    placeholder="loop_index"
                    value={step.loop_index_key || ''}
                    onCommit={(v) => update({ loop_index_key: v || undefined })}
                />
            </Field>

            <Field label="Stop early when" hint="Optional. Checked after each iteration.">
                <LocalInput
                    size="sm"
                    aria-label="Break condition"
                    placeholder="state.status == 'done'"
                    value={step.loop_break_condition || ''}
                    onCommit={(v) => update({ loop_break_condition: v || undefined })}
                />
            </Field>
            {/* The collection, while and break fields are all expressions (F957). */}
            <ExpressionVocabulary />

            <Field label="Max iterations" hint="Hard ceiling, whichever mode is used">
                <Input
                    size="sm"
                    type="number"
                    min={1}
                    value={step.loop_max_iterations ?? 50}
                    onChange={(e) => update({ loop_max_iterations: parseInt(e.target.value) || 50 })}
                />
            </Field>
            <div className="space-y-1.5">
                <span className="block text-sm font-medium text-text">Body steps <span className="text-xs font-normal text-text-faint">(in order, each iteration)</span></span>
                {(step.loop_step_ids || []).map((sid, idx) => (
                    <div key={idx} className="flex items-center gap-2">
                        <span className="w-4 shrink-0 text-[10px] text-text-faint">{idx + 1}.</span>
                        <div className="flex-1">
                            <StepTargetSelect
                                aria-label={`Body step ${idx + 1}`}
                                value={sid || undefined}
                                noneLabel="Select step…"
                                onChange={(id) => {
                                    const newIds = [...(step.loop_step_ids || [])];
                                    newIds[idx] = id || '';
                                    update({ loop_step_ids: newIds });
                                }}
                                otherSteps={otherSteps}
                            />
                        </div>
                        <button
                            type="button"
                            aria-label={`Remove body step ${idx + 1}`}
                            onClick={() => update({ loop_step_ids: (step.loop_step_ids || []).filter((_, i) => i !== idx) })}
                            className="text-text-faint transition-colors hover:text-danger"
                        >
                            <Trash2 size={12} aria-hidden />
                        </button>
                    </div>
                ))}
                <Button size="sm" variant="secondary" onClick={() => update({ loop_step_ids: [...(step.loop_step_ids || []), ''] })}>
                    <Plus size={12} aria-hidden /> Add body step
                </Button>
                <p className="text-[10px] text-text-faint">The &quot;done&quot; path is the green output handle, or Done path below.</p>
            </div>
        </div>
    );
}

function HumanSection({ step, update }: StepSectionProps) {
    const [channels, setChannels] = useState<any[]>([]);

    useEffect(() => {
        fetch('/api/messaging/channels')
            .then((r) => (r.ok ? r.json() : []))
            .then((d) => setChannels(Array.isArray(d) ? d : []))
            .catch(() => {});
    }, []);

    const NONE = '__browser__';
    const fields = step.human_fields || [];

    return (
        <div className="space-y-3">
            <Field
                label="Prompt for human"
                composite
                hint={
                    <>
                        {/* The engine fills a human prompt with its own single-brace pass and never
                            renders `{{ }}` — on purpose: the prompt can be posted to a messaging
                            channel, where a workspace variable would leak (D104). */}
                        Use <span className="font-code">{'{state.key}'}</span> for context —
                        type <span className="font-code">{'{s'}</span> to pick a key. <span className="font-code">{'{{ }}'}</span> is not
                        filled in here.
                    </>
                }
            >
                <MarkdownEditor
                    key={step.id}
                    compact
                    rows={3}
                    profile="legacy-state"
                    value={step.human_prompt || ''}
                    onChange={(human_prompt) => update({ human_prompt })}
                    placeholder="What should the user decide?"
                />
            </Field>

            <div className="space-y-1.5">
                <span className="block text-sm font-medium text-text">
                    Structured fields <span className="text-xs font-normal text-text-faint">(optional form shown to the person)</span>
                </span>
                {fields.map((field, idx) => (
                    <div key={idx} className="space-y-1.5 rounded-md border border-border bg-surface-2/40 p-2">
                        <div className="flex gap-1.5">
                            <LocalInput
                                className="font-code"
                                value={field.name}
                                aria-label={`Field ${idx + 1} name`}
                                placeholder="field_name"
                                onCommit={(name) => {
                                    const next = [...fields];
                                    next[idx] = { ...field, name };
                                    update({ human_fields: next });
                                }}
                            />
                            <Select
                                size="sm"
                                aria-label={`Field ${idx + 1} type`}
                                className="w-28 shrink-0"
                                // One table for every form that shows a field (F75). A stored
                                // spelling it knows (`text`) reads as its type without being
                                // rewritten; one it does not is shown as itself, never blank.
                                value={humanFieldType(field.type) ?? field.type}
                                onChange={(type) => {
                                    const next = [...fields];
                                    next[idx] = { ...field, type };
                                    update({ human_fields: next });
                                }}
                                options={humanFieldType(field.type) === null
                                    ? [...HUMAN_FIELD_TYPES, { value: field.type!, label: `unknown type \u201C${field.type}\u201D`, disabled: true }]
                                    : HUMAN_FIELD_TYPES}
                            />
                            <button
                                type="button"
                                aria-label={`Remove field ${idx + 1}`}
                                onClick={() => update({ human_fields: fields.filter((_, i) => i !== idx) })}
                                className="shrink-0 text-text-faint transition-colors hover:text-danger"
                            >
                                <Trash2 size={12} aria-hidden />
                            </button>
                        </div>
                        <LocalInput
                            value={field.label}
                            aria-label={`Field ${idx + 1} label`}
                            placeholder="Label shown to the person"
                            onCommit={(label) => {
                                const next = [...fields];
                                next[idx] = { ...field, label };
                                update({ human_fields: next });
                            }}
                        />
                        {humanFieldType(field.type) === 'choice' && (
                            // A dropdown's options, comma-separated; the person picks one (F455).
                            <LocalInput
                                value={humanFieldOptions(field).join(', ')}
                                aria-label={`Field ${idx + 1} options`}
                                placeholder="Options, comma-separated: Approve, Later"
                                onCommit={(text) => {
                                    const next = [...fields];
                                    next[idx] = { ...field, options: text.split(',').map((o) => o.trim()).filter(Boolean) };
                                    update({ human_fields: next });
                                }}
                            />
                        )}
                    </div>
                ))}
                <Button
                    size="sm"
                    variant="secondary"
                    onClick={() =>
                        update({ human_fields: [...fields, { name: `field_${fields.length + 1}`, type: 'string', label: '' }] })
                    }
                >
                    <Plus size={12} aria-hidden /> Add field
                </Button>
            </div>

            <Field
                label="Notify messaging channel"
                hint={step.human_channel_id ? 'First response wins — from the messaging app or the browser, whichever arrives first.' : undefined}
            >
                <Select
                    size="sm"
                    aria-label="Messaging channel"
                    value={step.human_channel_id || NONE}
                    onChange={(v) => update({ human_channel_id: v === NONE ? undefined : v })}
                    options={[
                        { value: NONE, label: 'Browser UI only' },
                        ...channels.map((ch: any) => ({
                            value: ch.id,
                            label: ch.name,
                            hint: `${ch.platform} · ${ch.status ?? 'stopped'}`,
                        })),
                    ]}
                />
            </Field>
            {step.human_channel_id && (
                <Field label="Timeout (seconds)" hint="How long to wait for a channel reply before falling back to the browser UI only.">
                    <Input
                        size="sm"
                        type="number"
                        min={60}
                        step={60}
                        value={step.human_timeout_seconds ?? 3600}
                        onChange={(e) => update({ human_timeout_seconds: parseInt(e.target.value) || 3600 })}
                    />
                </Field>
            )}
        </div>
    );
}

function TransformSection({ step, update, codeRunnerNote, readOnly }: StepSectionProps) {
    return (
        <div className="space-y-2">
            <Note>
                <strong>Python execution</strong> — {codeRunnerNote} Read shared state as{' '}
                <code className="font-code">state.key</code> or <code className="font-code">state[&quot;key&quot;]</code>, and
                assign to <code className="font-code">result</code> to write the output key.
            </Note>
            <Field label="Python code" composite>
                {/* Keyed by step, so undo never walks back into another step's code. */}
                <PythonEditor
                    key={step.id}
                    height={240}
                    readOnly={readOnly}
                    value={step.transform_code || ''}
                    onChange={(code) => update({ transform_code: code })}
                />
            </Field>
        </div>
    );
}

function ExtractJsonSection() {
    return (
        <div className="space-y-2">
            <Note>
                <strong>Extract JSON</strong> — parses JSON from input text. Handles markdown fences
                (<code className="font-code">```json</code>), raw JSON, and multiple objects. A single object is
                stored directly; multiple objects are stored as an array.
            </Note>
            <p className="text-[11px] text-text-faint">
                Set <em>Input keys</em> below to say which shared-state values to scan; the result lands in the <em>Output key</em>.
            </p>
        </div>
    );
}

function PrintSection({ step, update }: StepSectionProps) {
    return (
        <div className="space-y-2">
            <Note>
                <strong>Print</strong> — stores your text or markdown into shared state.
                Use <code className="font-code">{'{state.key}'}</code> to embed values from previous steps.
            </Note>
            <Field
                label="Content"
                composite
                hint={
                    <>
                        {/* Print fills in only the single-brace form, so that is all it offers —
                            and no `@`: a Print step is not a prompt, so nothing expands a vault
                            mention in one. */}
                        Use <span className="font-code">{'{state.key}'}</span> or <span className="font-code">{'{state.key.nested}'}</span> —
                        type <span className="font-code">{'{s'}</span> to pick a key. <span className="font-code">{'{{ }}'}</span> is not
                        filled in here.
                    </>
                }
            >
                <MarkdownEditor
                    key={step.id}
                    compact
                    rows={6}
                    profile="legacy-state-nested"
                    value={step.print_content || ''}
                    onChange={(print_content) => update({ print_content })}
                    placeholder={'# Summary\n\nThe analysis result is: {state.analysis_result}'}
                />
            </Field>
        </div>
    );
}

function IfElseSection({ step, update, otherSteps, stateKeys }: StepSectionProps) {
    return (
        <div className="space-y-3">
            <Note>
                <strong>If / Else</strong> — evaluates a condition against shared state and routes to
                the True or False path. Missing keys resolve to <code className="font-code">None</code>.
            </Note>
            <Field label="Condition" required hint={<>Restricted Python over <span className="font-code">state.key</span> / <span className="font-code">state.key.nested</span> — comparisons, <span className="font-code">and</span>/<span className="font-code">or</span>/<span className="font-code">not</span>, <span className="font-code">in</span>, <span className="font-code">str()</span>/<span className="font-code">len()</span> and the common string methods. No f-strings or comprehensions.</>}>
                <Input
                    size="sm"
                    className="font-code"
                    value={step.if_condition || ''}
                    onChange={(e) => update({ if_condition: e.target.value })}
                    placeholder="state.result.flag == True"
                />
            </Field>
            <ExpressionVocabulary />
            <StateKeyHelper stateKeys={stateKeys} onInsert={(token) => update({ if_condition: ((step.if_condition || '') + ' ' + token).trimStart() })} />
            <Field label="True path">
                <StepTargetSelect
                    aria-label="True path"
                    value={step.if_true_step_id}
                    onChange={(id) => update({ if_true_step_id: id })}
                    otherSteps={otherSteps}
                />
            </Field>
            <Field label="False path">
                <StepTargetSelect
                    aria-label="False path"
                    value={step.if_false_step_id}
                    onChange={(id) => update({ if_false_step_id: id })}
                    otherSteps={otherSteps}
                />
            </Field>
        </div>
    );
}

function SwitchSection({ step, update, otherSteps, stateKeys }: StepSectionProps) {
    return (
        <div className="space-y-3">
            <Note>
                <strong>Switch</strong> — evaluates an expression against shared state and matches the result
                to case values (as strings). Routes to the matching case or the default path.
            </Note>
            <Field label="Expression" required hint={<>Restricted Python over <span className="font-code">state.key</span> — same syntax as an If / Else condition. Build text with <span className="font-code">str(…)</span>, not an f-string.</>}>
                <Input
                    size="sm"
                    className="font-code"
                    value={step.switch_expression || ''}
                    onChange={(e) => update({ switch_expression: e.target.value })}
                    placeholder="state.result.status"
                />
            </Field>
            <ExpressionVocabulary />
            <StateKeyHelper stateKeys={stateKeys} onInsert={(token) => update({ switch_expression: ((step.switch_expression || '') + ' ' + token).trimStart() })} />
            <div className="space-y-2">
                <span className="block text-sm font-medium text-text">Cases <span className="text-xs font-normal text-text-faint">(value → target step)</span></span>
                {Object.entries(step.switch_cases || {}).map(([caseVal, targetId]) => (
                    <div key={caseVal} className="space-y-1.5 rounded-md border border-border bg-surface-2/40 p-2">
                        <LocalInput
                            className="font-code"
                            value={caseVal}
                            aria-label={`Case value ${caseVal}`}
                            placeholder="Match value"
                            onCommit={(newVal) => {
                                if (newVal === caseVal || !newVal.trim()) return;
                                update({
                                    switch_cases: Object.fromEntries(
                                        Object.entries(step.switch_cases || {}).map(([k, v]) => [k === caseVal ? newVal : k, v]),
                                    ),
                                });
                            }}
                        />
                        <StepTargetSelect
                            aria-label={`Target for case ${caseVal}`}
                            value={targetId}
                            noneLabel="End workflow"
                            onChange={(target) => update({ switch_cases: { ...(step.switch_cases || {}), [caseVal]: target ?? null } })}
                            otherSteps={otherSteps}
                        />
                        <Button
                            size="sm"
                            variant="ghost"
                            className="w-full text-danger hover:text-danger"
                            onClick={() => {
                                const newCases = { ...(step.switch_cases || {}) };
                                delete newCases[caseVal];
                                update({ switch_cases: newCases });
                            }}
                        >
                            <Trash2 size={12} aria-hidden /> Remove case
                        </Button>
                    </div>
                ))}
                <Button
                    size="sm"
                    variant="secondary"
                    onClick={() => {
                        const existing = Object.keys(step.switch_cases || {});
                        update({ switch_cases: { ...(step.switch_cases || {}), [`case_${existing.length + 1}`]: null } });
                    }}
                >
                    <Plus size={12} aria-hidden /> Add case
                </Button>
            </div>
            <Field label="Default path" hint="Taken when no case matches.">
                <StepTargetSelect
                    aria-label="Default path"
                    value={step.switch_default_step_id}
                    onChange={(id) => update({ switch_default_step_id: id })}
                    otherSteps={otherSteps}
                />
            </Field>
        </div>
    );
}

/* ── Knowledge: Retrieve and Ingest (R1) ────────────────────────────────── */

function isActive(kb: KnowledgeBaseOption): boolean {
    return (kb.status ?? 'active') === 'active';
}

/**
 * Knowledge bases picked by name. A base the step names that is gone or
 * disabled stays on it, flagged, rather than vanishing; without the list
 * (a shell that does not pass it) the ids are typed.
 */
function KnowledgeBasesPicker({ value, onChange, knowledgeBases }: {
    value: string[];
    onChange: (ids: string[]) => void;
    knowledgeBases?: KnowledgeBaseOption[];
}) {
    if (!knowledgeBases) return <KeyChips value={value} onChange={onChange} suggestions={[]} inputLabel="Add a knowledge base id" />;
    const remaining = knowledgeBases.filter((kb) => isActive(kb) && !value.includes(kb.id));
    return (
        <div className="space-y-1.5">
            {value.length > 0 && (
                <div className="flex flex-wrap gap-1">
                    {value.map((id) => {
                        const kb = knowledgeBases.find((b) => b.id === id);
                        const problem = !kb ? 'not found' : !isActive(kb) ? 'disabled' : null;
                        return (
                            <span
                                key={id}
                                title={id}
                                className={`inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-[11px] ${problem ? 'bg-danger-subtle text-danger' : 'bg-accent-subtle text-accent'}`}
                            >
                                {kb?.name ?? <span className="font-code" translate="no">{id}</span>}
                                {problem && <span>· {problem}</span>}
                                <button
                                    type="button"
                                    aria-label={`Remove ${kb?.name ?? id}`}
                                    onClick={() => onChange(value.filter((k) => k !== id))}
                                    className="rounded-sm opacity-70 transition-opacity hover:opacity-100"
                                >
                                    <Trash2 size={10} aria-hidden />
                                </button>
                            </span>
                        );
                    })}
                </div>
            )}
            {remaining.length > 0 ? (
                <Combobox
                    size="sm"
                    aria-label="Add a knowledge base"
                    value={undefined}
                    onChange={(id) => id && onChange([...value, id])}
                    options={remaining.map((kb) => ({ value: kb.id, label: kb.name, hint: kb.description || kb.id }))}
                    placeholder="Add a knowledge base…"
                    searchPlaceholder="Search knowledge bases…"
                />
            ) : knowledgeBases.filter(isActive).length === 0 ? (
                <p className="text-[11px] text-text-faint">No knowledge bases yet — make one on the Knowledge screen.</p>
            ) : null}
        </div>
    );
}

/** The filter as JSON, kept as typed: a value that is not a JSON object is shown, and not saved, until it is one. */
function FilterEditor({ value, onChange }: {
    value: RetrieveSettings['filters'];
    onChange: (filters: Record<string, unknown> | undefined) => void;
}) {
    const [text, setText] = useState(() => (value ? JSON.stringify(value, null, 2) : ''));
    let problem: string | null = null;
    if (text.trim()) {
        try {
            const parsed = JSON.parse(text);
            if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) problem = 'A filter is a JSON object.';
        } catch {
            problem = null; // the editor says what is wrong with the JSON itself
        }
    }
    return (
        <>
            <CodeEditor
                language="json"
                lint="json"
                height={96}
                value={text}
                onChange={(next) => {
                    setText(next);
                    if (!next.trim()) return onChange(undefined);
                    try {
                        const parsed = JSON.parse(next);
                        if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) onChange(parsed);
                    } catch {
                        // not JSON yet: keep what was saved
                    }
                }}
                placeholder={'{"doc_type": "contract", "year": {"$gte": 2024}}'}
                aria-label="Metadata filter"
            />
            {problem && <p className="text-[11px] text-danger">{problem}</p>}
        </>
    );
}

function RetrieveSection({ step, update, knowledgeBases }: StepSectionProps) {
    const settings: RetrieveSettings = step.retrieve || {};
    const set = (patch: Partial<RetrieveSettings>) => update({ retrieve: { ...settings, ...patch } });
    const kbIds = settings.kb_ids || [];
    const snapshot = settings.snapshot && settings.snapshot !== 'active' ? settings.snapshot : '';
    return (
        <div className="space-y-3">
            <Note>
                <strong>Retrieve</strong> — searches knowledge bases, with no model call. The output key holds{' '}
                <code className="font-code">context</code> (the passages under <code className="font-code">[K#]</code> labels,
                marked as documents), <code className="font-code">chunks</code>, <code className="font-code">citations</code> and{' '}
                <code className="font-code">trace_id</code>. Read the passages in a later prompt as{' '}
                <code className="font-code">{'{{ state.key.context }}'}</code>.
            </Note>
            <Field label="Knowledge bases" required composite>
                <KnowledgeBasesPicker
                    value={kbIds}
                    onChange={(kb_ids) => set({ kb_ids })}
                    knowledgeBases={knowledgeBases}
                />
            </Field>
            <Field
                label="Query"
                required
                hint={<>What to search for. Type <span className="font-code">{'{{'}</span> for state and variables — <span className="font-code">{'{{ state.question }}'}</span>.</>}
            >
                <TemplateInput
                    size="sm"
                    profile="step-prompt"
                    aria-label="Query"
                    value={settings.query || ''}
                    onChange={(e) => set({ query: e.target.value })}
                    placeholder="{{ state.user_input }}"
                />
            </Field>
            <Field
                label="Metadata filter"
                composite
                hint={<>Optional, over each document&apos;s metadata. <span className="font-code">$eq $ne $in $nin $gt $gte $lt $lte</span>, combined with <span className="font-code">$and $or $not</span>.</>}
            >
                <FilterEditor key={step.id} value={settings.filters} onChange={(filters) => set({ filters })} />
            </Field>
            <div className="grid grid-cols-2 gap-2">
                <Field label="Passages" hint="1–50. Blank: the knowledge base's own.">
                    <Input
                        size="sm"
                        type="number"
                        min={1}
                        max={50}
                        value={settings.top_k ?? ''}
                        onChange={(e) => set({ top_k: intOrUndefined(e.target.value) })}
                        placeholder="8"
                    />
                </Field>
                <Field label="Rerank">
                    <Select
                        size="sm"
                        aria-label="Rerank"
                        value={settings.rerank == null ? 'default' : settings.rerank ? 'on' : 'off'}
                        onChange={(v) => set({ rerank: v === 'default' ? undefined : v === 'on' })}
                        options={[
                            { value: 'default', label: 'Base default' },
                            { value: 'on', label: 'On' },
                            { value: 'off', label: 'Off' },
                        ]}
                    />
                </Field>
            </div>
            <Field
                label="Index version"
                hint={kbIds.length > 1
                    ? 'Searches each knowledge base’s version in use at run time. A pin names one base’s version, so it needs exactly one base.'
                    : 'Blank searches the version in use at run time. A ksnap_… id pins this step to one version, even after re-indexing.'}
            >
                <Input
                    size="sm"
                    className="font-code"
                    aria-label="Index version"
                    value={snapshot}
                    onChange={(e) => set({ snapshot: e.target.value.trim() || undefined })}
                    placeholder="active"
                />
            </Field>
        </div>
    );
}

function IngestSection({ step, update, stateKeys, knowledgeBases }: StepSectionProps) {
    const settings: IngestSettings = step.ingest || {};
    const set = (patch: Partial<IngestSettings>) => update({ ingest: { ...settings, ...patch } });
    const options = (knowledgeBases || []).filter((kb) => isActive(kb) || kb.id === settings.kb_id);
    return (
        <div className="space-y-3">
            <Note>
                <strong>Ingest</strong> — adds each object in a state key to a knowledge base as a record, with no model
                call. They are indexed after the step, so a Retrieve later in the same run may not find them yet. The
                output key lists each document and its status.
            </Note>
            <Field label="Knowledge base" required>
                {knowledgeBases ? (
                    <Combobox
                        size="sm"
                        aria-label="Knowledge base"
                        value={settings.kb_id}
                        onChange={(kb_id) => set({ kb_id: kb_id || undefined })}
                        options={options.map((kb) => ({
                            value: kb.id, label: kb.name, hint: isActive(kb) ? kb.description || kb.id : 'disabled',
                        }))}
                        placeholder="Select a knowledge base…"
                        searchPlaceholder="Search knowledge bases…"
                    />
                ) : (
                    <Input
                        size="sm"
                        className="font-code"
                        aria-label="Knowledge base"
                        value={settings.kb_id || ''}
                        onChange={(e) => set({ kb_id: e.target.value.trim() || undefined })}
                        placeholder="kb_…"
                    />
                )}
            </Field>
            <Field label="Records from" required hint="The state key holding a list of objects. An Extract JSON step makes one from a model's answer.">
                <Input
                    size="sm"
                    className="font-code"
                    aria-label="Records from"
                    value={settings.records_from || ''}
                    onChange={(e) => set({ records_from: e.target.value.trim() || undefined })}
                    placeholder="tickets"
                />
            </Field>
            <StateKeyHelper stateKeys={stateKeys} onInsert={(token) => set({ records_from: token.replace(/^state\./, '') })} />
            <Field label="Id field" hint="Names each record; the same id replaces its record. Blank: an id from the record's content, so the same record twice is one.">
                <Input
                    size="sm"
                    className="font-code"
                    aria-label="Id field"
                    value={settings.id_field || ''}
                    onChange={(e) => set({ id_field: e.target.value.trim() || undefined })}
                    placeholder="ticket_id"
                />
            </Field>
            <Field label="Text field" hint="The field holding the text to index. Blank: the whole object is indexed as JSON.">
                <Input
                    size="sm"
                    className="font-code"
                    aria-label="Text field"
                    value={settings.text_field || ''}
                    onChange={(e) => set({ text_field: e.target.value.trim() || undefined })}
                    placeholder="body"
                />
            </Field>
            <Field label="Metadata fields" composite hint="Copied into each record's metadata, so a Retrieve step can filter on them.">
                <KeyChips
                    value={settings.metadata_fields || []}
                    onChange={(metadata_fields) => set({ metadata_fields })}
                    suggestions={[]}
                    inputLabel="Add a metadata field"
                />
            </Field>
        </div>
    );
}

function EndSection() {
    return <p className="text-xs text-text-faint">This step ends the workflow. No configuration needed.</p>;
}

export const STEP_SECTIONS: Record<StepType, React.FC<StepSectionProps>> = {
    agent: AgentSection,
    llm: LlmSection,
    tool: ToolSection,
    evaluator: EvaluatorSection,
    parallel: ParallelSection,
    merge: MergeSection,
    loop: LoopSection,
    human: HumanSection,
    transform: TransformSection,
    extract_json: ExtractJsonSection,
    if_else: IfElseSection,
    switch: SwitchSection,
    print: PrintSection,
    end: EndSection,
    retrieve: RetrieveSection,
    ingest: IngestSection,
    workflow: WorkflowSection,
};
