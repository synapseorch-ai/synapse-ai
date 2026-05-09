'use client';
/* eslint-disable @typescript-eslint/no-explicit-any */
import { useState, useEffect, useRef } from 'react';
import { X, Plus, Trash2, MessageSquare } from 'lucide-react';
import { VaultTextarea } from '@/components/VaultMention';
import { EditorView } from '@codemirror/view';
import { basicSetup } from 'codemirror';
import { python } from '@codemirror/lang-python';
import { oneDarkTheme } from '@codemirror/theme-one-dark';
import { EditorState } from '@codemirror/state';
import { HighlightStyle, syntaxHighlighting } from '@codemirror/language';
import { tags } from '@lezer/highlight';
import type { StepConfig, StepType } from '@/types/orchestration';
import { STEP_TYPE_META } from '@/types/orchestration';

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
}

const STEP_TYPES: StepType[] = ['agent', 'llm', 'tool', 'evaluator', 'parallel', 'merge', 'loop', 'human', 'transform', 'extract_json', 'if_else', 'switch', 'print', 'end'];

export function StepConfigPanel({ step, agents, allStepIds, onUpdate, onDelete, onClose, isEntry, onSetEntry, availableModels }: StepConfigPanelProps) {
    const update = (patch: Partial<StepConfig>) => onUpdate({ ...step, ...patch });
    const otherSteps = allStepIds.filter((s) => s.id !== step.id);

    const inputCls = "w-full bg-zinc-900 border border-zinc-700 rounded px-3 py-1.5 text-sm text-zinc-200 focus:border-blue-500 outline-none";
    const inputSmCls = "w-full bg-zinc-900 border border-zinc-700 rounded px-3 py-1.5 text-xs text-zinc-200 font-mono focus:border-blue-500 outline-none";
    const textareaCls = "w-full bg-zinc-900 border border-zinc-700 rounded px-3 py-2 text-xs text-zinc-200 font-mono focus:border-blue-500 outline-none resize-y min-h-[80px]";
    const selectCls = "w-full bg-zinc-900 border border-zinc-700 rounded px-3 py-1.5 text-sm text-zinc-200 focus:border-blue-500 outline-none";

    return (
        <div className="w-80 bg-zinc-800 border-l border-zinc-700 overflow-y-auto flex flex-col">
            {/* Header */}
            <div className="flex items-center justify-between px-4 py-3 border-b border-zinc-700">
                <h3 className="text-sm font-semibold text-zinc-200">Step Config</h3>
                <button onClick={onClose} className="text-zinc-400 hover:text-zinc-200"><X size={16} /></button>
            </div>

            <div className="flex-1 overflow-y-auto p-4 space-y-4">
                {/* Name */}
                <div>
                    <label className="text-xs text-zinc-400 block mb-1">Name</label>
                    <input className={inputCls} value={step.name} onChange={(e) => update({ name: e.target.value })} />
                </div>

                {/* Type */}
                <div>
                    <label className="text-xs text-zinc-400 block mb-1">Type</label>
                    <select className={selectCls} value={step.type} onChange={(e) => update({ type: e.target.value as StepType })}>
                        {STEP_TYPES.map((t) => (
                            <option key={t} value={t}>{STEP_TYPE_META[t].label}</option>
                        ))}
                    </select>
                </div>

                {/* Entry point */}
                {!isEntry && (
                    <button onClick={onSetEntry} className="text-xs text-green-400 hover:text-green-300 underline">
                        Set as entry point
                    </button>
                )}
                {isEntry && <div className="text-xs text-green-400">This is the entry point</div>}

                {/* ===== AGENT config ===== */}
                {step.type === 'agent' && (
                    <>
                        <div>
                            <label className="text-xs text-zinc-400 block mb-1">Agent</label>
                            <select className={selectCls} value={step.agent_id || ''} onChange={(e) => update({ agent_id: e.target.value || undefined })}>
                                <option value="">Select agent...</option>
                                {agents.map((a: any) => <option key={a.id} value={a.id}>{a.name} ({a.type})</option>)}
                            </select>
                        </div>
                        <div>
                            <label className="text-xs text-zinc-400 block mb-1">Prompt Template</label>
                            <VaultTextarea className={textareaCls} rows={4} value={step.prompt_template || ''} onChange={(e) => update({ prompt_template: e.target.value })} placeholder="Use {state.key} to reference shared state, @ to reference vault files..." />
                        </div>
                    </>
                )}

                {/* ===== TOOL config ===== */}
                {step.type === 'tool' && (
                    <ToolStepConfig step={step} update={update} textareaCls={textareaCls} selectCls={selectCls} availableModels={availableModels} />
                )}

                {/* ===== LLM config ===== */}
                {step.type === 'llm' && (
                    <>
                        <div className="rounded bg-teal-950/40 border border-teal-800/40 px-3 py-2 text-[10px] text-teal-400 leading-relaxed">
                            <strong>Single LLM call</strong> — no agent, no tools. Great for summaries, rewrites, and lightweight reasoning between steps.
                        </div>
                        <div>
                            <label className="text-xs text-zinc-400 block mb-1">Prompt Template</label>
                            <VaultTextarea
                                className={textareaCls}
                                rows={5}
                                value={step.prompt_template || ''}
                                onChange={(e) => update({ prompt_template: e.target.value })}
                                placeholder={`Summarize the following in 3 bullet points:\n\n{state.analysis_result}\n\nType @ to reference a vault file`}
                            />
                            <p className="text-[10px] text-zinc-600 mt-0.5">Use {'{'+'state.key}'+'}'} to embed shared state values. Type <span className="text-emerald-400 font-mono">@</span> to reference a vault file.</p>
                        </div>
                        <div>
                            <label className="text-xs text-zinc-400 block mb-1">Model <span className="text-zinc-600 normal-case">(override)</span></label>
                            <select className={selectCls} value={step.model || ''} onChange={(e) => update({ model: e.target.value || undefined })}>
                                <option value="">(Default)</option>
                                {(availableModels || []).map((m) => (
                                    <option key={m} value={m}>{m}</option>
                                ))}
                            </select>
                        </div>
                    </>
                )}

                {/* ===== EVALUATOR config ===== */}
                {step.type === 'evaluator' && (
                    <>
                        <div>
                            <label className="text-xs text-zinc-400 block mb-1">Evaluator Prompt <span className="text-zinc-600 normal-case">(routing decision)</span></label>
                            <textarea className={textareaCls} rows={3} value={step.evaluator_prompt || ''} onChange={(e) => update({ evaluator_prompt: e.target.value })} placeholder="Instructions for the routing decision (e.g. If login is needed, route to human...)" />
                        </div>
                        <div>
                            <label className="text-xs text-zinc-400 block mb-1">Model <span className="text-zinc-600 normal-case">(override for evaluator)</span></label>
                            <select className={selectCls} value={step.model || ''} onChange={(e) => update({ model: e.target.value || undefined })}>
                                <option value="">(Default)</option>
                                {(availableModels || []).map((m) => (
                                    <option key={m} value={m}>{m}</option>
                                ))}
                            </select>
                        </div>
                        <div>
                            <label className="text-xs text-zinc-400 block mb-1">Routes <span className="text-zinc-600 normal-case">(LLM picks one based on context)</span></label>
                            <div className="space-y-2">
                                {Object.entries(step.route_map || {}).map(([label, targetId]) => (
                                    <RouteEntry
                                        key={label}
                                        label={label}
                                        targetId={targetId}
                                        description={(step.route_descriptions || {})[label] || ''}
                                        otherSteps={otherSteps}
                                        onRename={(newLabel) => {
                                            if (newLabel === label || !newLabel.trim()) return;
                                            const entries = Object.entries(step.route_map || {});
                                            const newMap: Record<string, string | null> = {};
                                            for (const [k, v] of entries) {
                                                newMap[k === label ? newLabel : k] = v;
                                            }
                                            // Also rename in route_descriptions
                                            const descEntries = Object.entries(step.route_descriptions || {});
                                            const newDescs: Record<string, string> = {};
                                            for (const [k, v] of descEntries) {
                                                newDescs[k === label ? newLabel : k] = v;
                                            }
                                            update({ route_map: newMap, route_descriptions: newDescs });
                                        }}
                                        onChangeTarget={(val) => {
                                            update({ route_map: { ...(step.route_map || {}), [label]: val } });
                                        }}
                                        onChangeDescription={(desc) => {
                                            update({ route_descriptions: { ...(step.route_descriptions || {}), [label]: desc } });
                                        }}
                                        onDelete={() => {
                                            const newMap = { ...(step.route_map || {}) };
                                            delete newMap[label];
                                            const newDescs = { ...(step.route_descriptions || {}) };
                                            delete newDescs[label];
                                            update({ route_map: newMap, route_descriptions: newDescs });
                                        }}
                                    />
                                ))}
                                <button
                                    onClick={() => {
                                        const existing = Object.keys(step.route_map || {});
                                        const newLabel = `route_${existing.length + 1}`;
                                        update({ route_map: { ...(step.route_map || {}), [newLabel]: null } });
                                    }}
                                    className="flex items-center gap-1 text-xs text-emerald-400 hover:text-emerald-300"
                                >
                                    <Plus size={12} /> Add Route
                                </button>
                            </div>
                        </div>
                    </>
                )}

                {/* ===== PARALLEL config ===== */}
                {step.type === 'parallel' && (
                    <div>
                        <label className="text-xs text-zinc-400 block mb-1">Branches (pick entry step per branch)</label>
                        <div className="space-y-2">
                            {(step.parallel_branches || []).map((branch, branchIdx) => (
                                <div key={branchIdx} className="flex items-center gap-2">
                                    <span className="text-[10px] text-purple-400 font-semibold w-5">B{branchIdx + 1}</span>
                                    <select
                                        className="flex-1 bg-zinc-900 border border-zinc-700 rounded px-2 py-1 text-xs text-zinc-200 outline-none"
                                        value={branch[0] || ''}
                                        onChange={(e) => {
                                            const newBranches = [...(step.parallel_branches || [])];
                                            newBranches[branchIdx] = e.target.value ? [e.target.value] : [];
                                            update({ parallel_branches: newBranches });
                                        }}
                                    >
                                        <option value="">Select entry step...</option>
                                        {otherSteps.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
                                    </select>
                                    <button
                                        onClick={() => {
                                            const newBranches = (step.parallel_branches || []).filter((_, i) => i !== branchIdx);
                                            update({ parallel_branches: newBranches });
                                        }}
                                        className="text-red-400 hover:text-red-300"
                                    >
                                        <Trash2 size={10} />
                                    </button>
                                </div>
                            ))}
                            <button
                                onClick={() => update({ parallel_branches: [...(step.parallel_branches || []), []] })}
                                className="flex items-center gap-1 text-xs text-purple-400 hover:text-purple-300"
                            >
                                <Plus size={12} /> Add Branch
                            </button>
                        </div>
                        <p className="text-[10px] text-zinc-500 mt-1">Each branch auto-follows the entry step&apos;s Next Step chain. Connect steps with edges on the canvas.</p>
                    </div>
                )}

                {/* ===== MERGE config ===== */}
                {step.type === 'merge' && (
                    <>
                        <div>
                            <label className="text-xs text-zinc-400 block mb-1">Merge Strategy</label>
                            <select className={selectCls} value={step.merge_strategy || 'list'} onChange={(e) => update({ merge_strategy: e.target.value as any })}>
                                <option value="list">List (array of sources)</option>
                                <option value="concat">Concat (text join)</option>
                                <option value="dict">Dict (keyed by source)</option>
                            </select>
                        </div>
                    </>
                )}

                {/* ===== LOOP config ===== */}
                {step.type === 'loop' && (
                    <>
                        <div>
                            <label className="text-xs text-zinc-400 block mb-1">Loop Count</label>
                            <input
                                type="number"
                                className={inputCls}
                                value={step.loop_count ?? 3}
                                onChange={(e) => update({ loop_count: parseInt(e.target.value) || 3 })}
                                min={1}
                            />
                        </div>
                        <div>
                            <label className="text-xs text-zinc-400 block mb-1">Body Steps (executed in order each iteration)</label>
                            <div className="space-y-1">
                                {(step.loop_step_ids || []).map((sid, idx) => (
                                    <div key={idx} className="flex items-center gap-1">
                                        <span className="text-[10px] text-amber-400 w-4">{idx + 1}.</span>
                                        <select
                                            className="flex-1 bg-zinc-900 border border-zinc-700 rounded px-2 py-0.5 text-xs text-zinc-200 outline-none"
                                            value={sid}
                                            onChange={(e) => {
                                                const newIds = [...(step.loop_step_ids || [])];
                                                newIds[idx] = e.target.value;
                                                update({ loop_step_ids: newIds });
                                            }}
                                        >
                                            <option value="">Select step...</option>
                                            {otherSteps.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
                                        </select>
                                        <button
                                            onClick={() => {
                                                const newIds = (step.loop_step_ids || []).filter((_, i) => i !== idx);
                                                update({ loop_step_ids: newIds });
                                            }}
                                            className="text-red-400 hover:text-red-300"
                                        >
                                            <Trash2 size={10} />
                                        </button>
                                    </div>
                                ))}
                                <button
                                    onClick={() => update({ loop_step_ids: [...(step.loop_step_ids || []), ''] })}
                                    className="flex items-center gap-1 text-xs text-amber-400 hover:text-amber-300"
                                >
                                    <Plus size={12} /> Add Body Step
                                </button>
                            </div>
                            <p className="text-[10px] text-zinc-500 mt-1">The &quot;done&quot; path is configured via the green output handle or Next Step below.</p>
                        </div>
                    </>
                )}

                {/* ===== HUMAN config ===== */}
                {step.type === 'human' && (
                    <HumanStepConfig step={step} update={update} textareaCls={textareaCls} inputCls={inputCls} selectCls={selectCls} />
                )}

                {/* ===== TRANSFORM config ===== */}
                {step.type === 'transform' && (
                    <div className="space-y-2">
                        <div className="rounded bg-amber-950/40 border border-amber-800/40 px-3 py-2 text-[10px] text-amber-400 leading-relaxed">
                            <strong>Python sandbox</strong> — runs in Docker (512MB RAM, no network). <code>state</code> dict is injected. Assign to <code>result</code> to write the output key.
                        </div>
                        <label className="text-xs text-zinc-400 block mb-1">Python Code</label>
                        <div className="border border-zinc-700 rounded overflow-hidden h-[220px] focus-within:border-amber-600 transition-colors">
                            <PythonCodeEditor
                                value={step.transform_code || ''}
                                onChange={(code) => update({ transform_code: code })}
                            />
                        </div>
                    </div>
                )}

                {/* ===== END config ===== */}
                {step.type === 'end' && (
                    <div className="text-xs text-zinc-500">This node terminates the orchestration. No configuration needed.</div>
                )}

                {/* ===== EXTRACT JSON config ===== */}
                {step.type === 'extract_json' && (
                    <div className="space-y-2">
                        <div className="rounded bg-orange-950/40 border border-orange-800/40 px-3 py-2 text-[10px] text-orange-400 leading-relaxed">
                            <strong>Extract JSON</strong> — parses JSON from input text. Handles markdown fences (<code>```json</code>), raw JSON, and multiple objects. Single object is stored directly; multiple objects are stored as an array.
                        </div>
                        <p className="text-[10px] text-zinc-500">Configure <em>Input Keys</em> below to specify which shared state values to scan. The extracted JSON will be stored in the <em>Output Key</em>.</p>
                    </div>
                )}

                {/* ===== PRINT config ===== */}
                {step.type === 'print' && (
                    <div className="space-y-2">
                        <div className="rounded bg-lime-950/40 border border-lime-800/40 px-3 py-2 text-[10px] text-lime-400 leading-relaxed">
                            <strong>Print</strong> — stores your text or markdown into shared state. Use <code>{'{state.key}'}</code> to embed values from previous steps.
                        </div>
                        <div>
                            <label className="text-xs text-zinc-400 block mb-1">Content</label>
                            <VaultTextarea
                                className={textareaCls}
                                rows={6}
                                value={step.print_content || ''}
                                onChange={(e) => update({ print_content: e.target.value })}
                                placeholder={`# Summary\n\nThe analysis result is: {state.analysis_result}\n\nStatus: {state.status}\n\nType @ to reference a vault file`}
                            />
                            <p className="text-[10px] text-zinc-600 mt-0.5">
                                Use <code className="text-lime-400">{'{state.key}'}</code> or <code className="text-lime-400">{'{state.key.nested}'}</code> to embed shared state values. Supports markdown. Type <span className="text-emerald-400 font-mono">@</span> to reference a vault file.
                            </p>
                        </div>
                    </div>
                )}

                {/* ===== IF/ELSE config ===== */}
                {step.type === 'if_else' && (
                    <IfElseStepConfig step={step} update={update} otherSteps={otherSteps} inputCls={inputCls} selectCls={selectCls} />
                )}

                {/* ===== SWITCH config ===== */}
                {step.type === 'switch' && (
                    <SwitchStepConfig step={step} update={update} otherSteps={otherSteps} inputCls={inputCls} selectCls={selectCls} />
                )}

                <hr className="border-zinc-700" />

                {/* I/O mapping — not for end nodes */}
                {step.type !== 'end' && (
                    <>
                        <div>
                            <label className="text-xs text-zinc-400 block mb-1">Input Keys (comma-separated)</label>
                            <LocalInput
                                className={inputSmCls}
                                value={(step.input_keys || []).join(', ')}
                                onCommit={(val) => update({ input_keys: val.split(',').map((s) => s.trim()).filter(Boolean) })}
                                placeholder="portfolio_status, news_data"
                            />
                        </div>
                        <div>
                            <label className="text-xs text-zinc-400 block mb-1">Output Key</label>
                            <input
                                className={inputSmCls}
                                value={step.output_key || ''}
                                onChange={(e) => update({ output_key: e.target.value || undefined })}
                                placeholder="analysis_result"
                            />
                        </div>
                    </>
                )}

                {/* Next step — only for types that use linear routing (not evaluator with routes, not end, not loop via done handle) */}
                {step.type !== 'end' && step.type !== 'evaluator' && step.type !== 'loop' && step.type !== 'if_else' && step.type !== 'switch' && (
                    <div>
                        <label className="text-xs text-zinc-400 block mb-1">Next Step</label>
                        <select className={selectCls} value={step.next_step_id || ''} onChange={(e) => update({ next_step_id: e.target.value || undefined })}>
                            <option value="">None (end)</option>
                            {otherSteps.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
                        </select>
                    </div>
                )}

                {/* Loop "done" path shown as Next Step */}
                {step.type === 'loop' && (
                    <div>
                        <label className="text-xs text-zinc-400 block mb-1">Done Path (after all iterations)</label>
                        <select className={selectCls} value={step.next_step_id || ''} onChange={(e) => update({ next_step_id: e.target.value || undefined })}>
                            <option value="">None (end)</option>
                            {otherSteps.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
                        </select>
                    </div>
                )}

                <hr className="border-zinc-700" />

                {/* Guardrails — not for end nodes */}
                {step.type !== 'end' && (
                    <>
                        <div className="text-xs text-zinc-500 font-semibold uppercase tracking-wider">Guardrails</div>
                        <div className="grid grid-cols-2 gap-2">
                            <div>
                                <label className="text-xs text-zinc-400 block mb-1">Max Turns</label>
                                <input
                                    type="number"
                                    className="w-full bg-zinc-900 border border-zinc-700 rounded px-2 py-1 text-sm text-zinc-200 outline-none"
                                    value={step.max_turns ?? ''}
                                    placeholder="15"
                                    onChange={(e) => update({ max_turns: e.target.value === '' ? undefined : parseInt(e.target.value) })}
                                />
                            </div>
                            <div>
                                <label className="text-xs text-zinc-400 block mb-1">Timeout (s)</label>
                                <input
                                    type="number"
                                    className="w-full bg-zinc-900 border border-zinc-700 rounded px-2 py-1 text-sm text-zinc-200 outline-none"
                                    value={step.timeout_seconds ?? ''}
                                    placeholder="300"
                                    onChange={(e) => update({ timeout_seconds: e.target.value === '' ? undefined : parseInt(e.target.value) })}
                                />
                            </div>
                        </div>
                        <div>
                            <label className="text-xs text-zinc-400 block mb-1">Max Iterations (loop guard)</label>
                            <input
                                type="number"
                                className="w-full bg-zinc-900 border border-zinc-700 rounded px-2 py-1 text-sm text-zinc-200 outline-none"
                                value={step.max_iterations ?? ''}
                                placeholder="3"
                                onChange={(e) => update({ max_iterations: e.target.value === '' ? undefined : parseInt(e.target.value) })}
                            />
                        </div>
                    </>
                )}
            </div>

            {/* Footer */}
            <div className="border-t border-zinc-700 p-3">
                <button
                    onClick={onDelete}
                    className="w-full py-1.5 text-xs text-red-400 hover:text-red-300 hover:bg-red-900/20 rounded transition-colors"
                >
                    Delete Step
                </button>
            </div>
        </div>
    );
}

/** Human Step config — prompt + optional messaging channel + timeout */
function HumanStepConfig({ step, update, textareaCls, inputCls, selectCls }: {
    step: StepConfig;
    update: (patch: Partial<StepConfig>) => void;
    textareaCls: string;
    inputCls: string;
    selectCls: string;
}) {
    const [channels, setChannels] = useState<any[]>([]);

    useEffect(() => {
        fetch('/api/messaging/channels')
            .then(r => r.ok ? r.json() : [])
            .then(d => setChannels(Array.isArray(d) ? d : []))
            .catch(() => {});
    }, []);

    const PLATFORM_EMOJI: Record<string, string> = {
        telegram: '✈️', discord: '🎮', slack: '💬', teams: '📘', whatsapp: '📱',
    };

    return (
        <div className="space-y-3">
            <div>
                <label className="text-xs text-zinc-400 block mb-1">Prompt for Human</label>
                <textarea
                    className={textareaCls}
                    rows={3}
                    value={step.human_prompt || ''}
                    onChange={(e) => update({ human_prompt: e.target.value })}
                    placeholder="What should the user decide? Use {state.key} for context."
                />
                <p className="text-[10px] text-zinc-600 mt-0.5">Use {'{'+'state.key}'+'}'} to embed shared state values.</p>
            </div>

            <div>
                <label className="text-xs text-zinc-400 flex items-center gap-1 mb-1">
                    <MessageSquare size={11} /> Notify Messaging Channel <span className="text-zinc-600">(optional)</span>
                </label>
                <select
                    className={selectCls}
                    value={(step as any).human_channel_id || ''}
                    onChange={(e) => update({ human_channel_id: e.target.value || undefined } as any)}
                >
                    <option value="">Browser UI only</option>
                    {channels.map((ch: any) => (
                        <option key={ch.id} value={ch.id}>
                            {PLATFORM_EMOJI[ch.platform] ?? '🤖'} {ch.name} [{ch.status ?? 'stopped'}]
                        </option>
                    ))}
                </select>
                {(step as any).human_channel_id && (
                    <p className="text-[10px] text-amber-400 mt-1">
                        ⏱ First response wins — from messaging app <em>or</em> browser, whichever arrives first.
                    </p>
                )}
            </div>

            {(step as any).human_channel_id && (
                <div>
                    <label className="text-xs text-zinc-400 block mb-1">Timeout (seconds)</label>
                    <input
                        type="number"
                        className={inputCls}
                        value={(step as any).human_timeout_seconds ?? 3600}
                        onChange={(e) => update({ human_timeout_seconds: parseInt(e.target.value) || 3600 } as any)}
                        min={60}
                        step={60}
                    />
                    <p className="text-[10px] text-zinc-600 mt-0.5">How long to wait for a reply from the messaging channel before falling back to the browser UI only.</p>
                </div>
            )}
        </div>
    );
}

/** Tool Step config — single tool picker + prompt template + model override. */
function ToolStepConfig({ step, update, textareaCls, selectCls, availableModels }: {
    step: StepConfig;
    update: (patch: Partial<StepConfig>) => void;
    textareaCls: string;
    selectCls: string;
    availableModels?: string[];
}) {
    const [availableTools, setAvailableTools] = useState<{ name: string; description: string }[]>([]);

    useEffect(() => {
        fetch('/api/tools/available')
            .then(r => r.ok ? r.json() : { tools: [] })
            .then(d => setAvailableTools(Array.isArray(d.tools) ? d.tools : []))
            .catch(() => {});
    }, []);

    const selectedTool = availableTools.find(t => t.name === step.forced_tool);

    return (
        <div className="space-y-3">
            <div className="rounded bg-purple-950/40 border border-purple-800/40 px-3 py-2 text-[10px] text-purple-400 leading-relaxed">
                <strong>Forced tool call</strong> — the LLM generates arguments for exactly one tool, then calls it. If the first attempt fails, the ReAct loop retries up to <em>Max Turns</em> times.
            </div>
            <div>
                <label className="text-xs text-zinc-400 block mb-1">Tool</label>
                <select
                    className={selectCls}
                    value={step.forced_tool || ''}
                    onChange={(e) => update({ forced_tool: e.target.value || undefined })}
                >
                    <option value="">Select tool...</option>
                    {availableTools.map(t => (
                        <option key={t.name} value={t.name}>{t.name}</option>
                    ))}
                </select>
                {selectedTool?.description && (
                    <p className="text-[10px] text-zinc-500 mt-0.5">{selectedTool.description}</p>
                )}
            </div>
            <div>
                <label className="text-xs text-zinc-400 block mb-1">Prompt Template</label>
                <VaultTextarea
                    className={textareaCls}
                    rows={4}
                    value={step.prompt_template || ''}
                    onChange={(e) => update({ prompt_template: e.target.value })}
                    placeholder={`Search for relevant data about {state.user_input}\n\nType @ to reference a vault file`}
                />
                <p className="text-[10px] text-zinc-600 mt-0.5">Use {'{'+'state.key}'+'}'} to embed shared state. Type <span className="text-emerald-400 font-mono">@</span> to reference a vault file.</p>
            </div>
            <div>
                <label className="text-xs text-zinc-400 block mb-1">Model <span className="text-zinc-600 normal-case">(override)</span></label>
                <select className={selectCls} value={step.model || ''} onChange={(e) => update({ model: e.target.value || undefined })}>
                    <option value="">(Default)</option>
                    {(availableModels || []).map((m) => (
                        <option key={m} value={m}>{m}</option>
                    ))}
                </select>
            </div>
        </div>
    );
}

/** Text input that buffers locally and only commits on blur — prevents mid-type parsing (e.g. comma in CSV fields). */
function LocalInput({ value, onCommit, ...props }: { value: string; onCommit: (val: string) => void } & React.InputHTMLAttributes<HTMLInputElement>) {
    const [local, setLocal] = useState(value);
    useEffect(() => { setLocal(value); }, [value]);
    return (
        <input
            {...props}
            value={local}
            onChange={(e) => setLocal(e.target.value)}
            onBlur={() => onCommit(local)}
        />
    );
}

/** Syntax-highlighted Python code editor (CodeMirror) for the transform step. */
const pythonHighlight = syntaxHighlighting(HighlightStyle.define([
    { tag: tags.keyword, color: '#c792ea', fontWeight: 'bold' },
    { tag: tags.definitionKeyword, color: '#c792ea', fontWeight: 'bold' },
    { tag: tags.self, color: '#f78c6c', fontStyle: 'italic' },
    { tag: tags.bool, color: '#ff9cac' },
    { tag: tags.null, color: '#ff9cac' },
    { tag: tags.definition(tags.function(tags.variableName)), color: '#82aaff', fontWeight: 'bold' },
    { tag: tags.function(tags.variableName), color: '#82aaff' },
    { tag: tags.definition(tags.className), color: '#ffcb6b', fontWeight: 'bold' },
    { tag: tags.className, color: '#ffcb6b' },
    { tag: tags.meta, color: '#ffa759', fontStyle: 'italic' },
    { tag: tags.variableName, color: '#eeffff' },
    { tag: tags.propertyName, color: '#89ddff' },
    { tag: tags.string, color: '#c3e88d' },
    { tag: tags.special(tags.string), color: '#c3e88d' },
    { tag: tags.number, color: '#f78c6c' },
    { tag: tags.operator, color: '#89ddff' },
    { tag: tags.punctuation, color: '#89ddff' },
    { tag: tags.bracket, color: '#ffcb6b' },
    { tag: tags.comment, color: '#546e7a', fontStyle: 'italic' },
    { tag: tags.typeName, color: '#ffcb6b' },
    { tag: tags.escape, color: '#f78c6c' },
]));

function PythonCodeEditor({ value, onChange }: { value: string; onChange: (code: string) => void }) {
    const containerRef = useRef<HTMLDivElement>(null);
    const editorRef = useRef<EditorView | null>(null);
    const onChangeRef = useRef(onChange);
    onChangeRef.current = onChange;

    useEffect(() => {
        if (!containerRef.current || editorRef.current) return;
        const state = EditorState.create({
            doc: value,
            extensions: [
                basicSetup,
                python(),
                oneDarkTheme,
                pythonHighlight,
                EditorView.updateListener.of((update) => {
                    if (update.docChanged) onChangeRef.current(update.state.doc.toString());
                }),
                EditorView.theme({
                    '&': { backgroundColor: '#09090b', height: '100%' },
                    '.cm-scroller': { overflow: 'auto', fontFamily: 'monospace', fontSize: '12px' },
                    '.cm-content': { padding: '8px 0' },
                    '.cm-line': { padding: '0 12px' },
                    '&.cm-focused .cm-cursor': { borderLeftColor: '#d97706' },
                    '.cm-selectionBackground': { backgroundColor: '#3f3f46' },
                    '&.cm-focused .cm-selectionBackground': { backgroundColor: '#78350f' },
                }),
            ],
        });
        editorRef.current = new EditorView({ state, parent: containerRef.current });
        return () => { editorRef.current?.destroy(); editorRef.current = null; };
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);

    // Sync external value changes (e.g. step switching)
    useEffect(() => {
        const view = editorRef.current;
        if (!view) return;
        const current = view.state.doc.toString();
        if (current !== value) {
            view.dispatch({ changes: { from: 0, to: current.length, insert: value } });
        }
    }, [value]);

    return <div ref={containerRef} className="h-full" />;
}

/** IF/Else Step config — condition + true/false path pickers. */
function IfElseStepConfig({ step, update, otherSteps, inputCls, selectCls }: {
    step: StepConfig;
    update: (patch: Partial<StepConfig>) => void;
    otherSteps: { id: string; name: string }[];
    inputCls: string;
    selectCls: string;
}) {
    return (
        <div className="space-y-3">
            <div className="rounded bg-yellow-950/40 border border-yellow-800/40 px-3 py-2 text-[10px] text-yellow-400 leading-relaxed">
                <strong>If / Else</strong> — evaluates a Python condition against shared state. Routes to the True or False path based on the result.
            </div>
            <div>
                <label className="text-xs text-zinc-400 block mb-1">Condition <span className="text-zinc-600 normal-case">(Python expression)</span></label>
                <input
                    className={`${inputCls} !font-mono`}
                    value={step.if_condition || ''}
                    onChange={(e) => update({ if_condition: e.target.value })}
                    placeholder="state.result.flag == True"
                />
                <p className="text-[10px] text-zinc-600 mt-0.5">
                    Use <code className="text-yellow-400">state.key</code> or <code className="text-yellow-400">state.key.nested</code> to access shared state. Missing keys resolve to <code>None</code>.
                </p>
            </div>
            <div>
                <label className="text-xs text-zinc-400 block mb-1">True Path <span className="text-green-500">(condition is True)</span></label>
                <select className={selectCls} value={step.if_true_step_id || ''} onChange={(e) => update({ if_true_step_id: e.target.value || undefined })}>
                    <option value="">None (end)</option>
                    {otherSteps.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
                </select>
            </div>
            <div>
                <label className="text-xs text-zinc-400 block mb-1">False Path <span className="text-red-500">(condition is False)</span></label>
                <select className={selectCls} value={step.if_false_step_id || ''} onChange={(e) => update({ if_false_step_id: e.target.value || undefined })}>
                    <option value="">None (end)</option>
                    {otherSteps.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
                </select>
            </div>
        </div>
    );
}

/** Switch Step config — expression + case entries + default path. UI mirrors evaluator routes. */
function SwitchStepConfig({ step, update, otherSteps, inputCls, selectCls }: {
    step: StepConfig;
    update: (patch: Partial<StepConfig>) => void;
    otherSteps: { id: string; name: string }[];
    inputCls: string;
    selectCls: string;
}) {
    return (
        <div className="space-y-3">
            <div className="rounded bg-cyan-950/40 border border-cyan-800/40 px-3 py-2 text-[10px] text-cyan-400 leading-relaxed">
                <strong>Switch</strong> — evaluates an expression against shared state and matches the result to case values. Routes to the matching case or the default path.
            </div>
            <div>
                <label className="text-xs text-zinc-400 block mb-1">Expression <span className="text-zinc-600 normal-case">(Python expression)</span></label>
                <input
                    className={`${inputCls} !font-mono`}
                    value={step.switch_expression || ''}
                    onChange={(e) => update({ switch_expression: e.target.value })}
                    placeholder="state.result.status"
                />
                <p className="text-[10px] text-zinc-600 mt-0.5">
                    Use <code className="text-cyan-400">state.key</code> to access shared state. Result is converted to string for matching.
                </p>
            </div>
            <div>
                <label className="text-xs text-zinc-400 block mb-1">Cases <span className="text-zinc-600 normal-case">(value → target step)</span></label>
                <div className="space-y-2">
                    {Object.entries(step.switch_cases || {}).map(([caseVal, targetId]) => (
                        <SwitchCaseEntry
                            key={caseVal}
                            caseValue={caseVal}
                            targetId={targetId}
                            otherSteps={otherSteps}
                            onRenameCase={(newVal) => {
                                if (newVal === caseVal || !newVal.trim()) return;
                                const entries = Object.entries(step.switch_cases || {});
                                const newCases: Record<string, string | null> = {};
                                for (const [k, v] of entries) {
                                    newCases[k === caseVal ? newVal : k] = v;
                                }
                                update({ switch_cases: newCases });
                            }}
                            onChangeTarget={(val) => {
                                update({ switch_cases: { ...(step.switch_cases || {}), [caseVal]: val } });
                            }}
                            onDelete={() => {
                                const newCases = { ...(step.switch_cases || {}) };
                                delete newCases[caseVal];
                                update({ switch_cases: newCases });
                            }}
                        />
                    ))}
                    <button
                        onClick={() => {
                            const existing = Object.keys(step.switch_cases || {});
                            const newVal = `case_${existing.length + 1}`;
                            update({ switch_cases: { ...(step.switch_cases || {}), [newVal]: null } });
                        }}
                        className="flex items-center gap-1 text-xs text-cyan-400 hover:text-cyan-300"
                    >
                        <Plus size={12} /> Add Case
                    </button>
                </div>
            </div>
            <div>
                <label className="text-xs text-zinc-400 block mb-1">Default Path <span className="text-zinc-600 normal-case">(no case matched)</span></label>
                <select className={selectCls} value={step.switch_default_step_id || ''} onChange={(e) => update({ switch_default_step_id: e.target.value || undefined })}>
                    <option value="">None (end)</option>
                    {otherSteps.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
                </select>
            </div>
        </div>
    );
}

/** Switch case entry — editable value + target picker. */
function SwitchCaseEntry({
    caseValue, targetId, otherSteps, onRenameCase, onChangeTarget, onDelete,
}: {
    caseValue: string;
    targetId: string | null;
    otherSteps: { id: string; name: string }[];
    onRenameCase: (newVal: string) => void;
    onChangeTarget: (val: string | null) => void;
    onDelete: () => void;
}) {
    const [localVal, setLocalVal] = useState(caseValue);

    return (
        <div className="bg-zinc-900 rounded p-2 space-y-2">
            <input
                className="w-full bg-zinc-800 border border-zinc-700 rounded px-2 py-1 text-xs text-zinc-200 font-mono outline-none"
                value={localVal}
                onChange={(e) => setLocalVal(e.target.value)}
                onBlur={() => onRenameCase(localVal)}
                onKeyDown={(e) => { if (e.key === 'Enter') onRenameCase(localVal); }}
                placeholder="Match value"
            />
            <select
                className="w-full bg-zinc-800 border border-zinc-700 rounded px-2 py-1 text-xs text-zinc-200 outline-none"
                value={targetId ?? '__end__'}
                onChange={(e) => onChangeTarget(e.target.value === '__end__' ? null : e.target.value)}
            >
                <option value="__end__">End Orchestration</option>
                {otherSteps.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
            </select>
            <div className="text-right">
                <button
                    onClick={onDelete}
                    className="w-full py-1 text-xs font-semibold text-red-400 bg-red-900/5 border border-red-700 rounded hover:bg-red-900/20 hover:text-red-300 transition-colors"
                    title="Delete this case"
                >
                    <Trash2 size={12} className="inline-block mr-1" /> Delete Case
                </button>
            </div>
        </div>
    );
}

/** Route label editor — uses local state so typing doesn't cause focus loss. */
function RouteEntry({
    label, targetId, description, otherSteps, onRename, onChangeTarget, onChangeDescription, onDelete,
}: {
    label: string;
    targetId: string | null;
    description: string;
    otherSteps: { id: string; name: string }[];
    onRename: (newLabel: string) => void;
    onChangeTarget: (val: string | null) => void;
    onChangeDescription: (desc: string) => void;
    onDelete: () => void;
}) {
    const [localLabel, setLocalLabel] = useState(label);
    const [localDesc, setLocalDesc] = useState(description);

    return (
        <div className="bg-zinc-900 rounded p-2 space-y-2">
            <div className="space-y-2">
                <input
                    className="w-full bg-zinc-800 border border-zinc-700 rounded px-2 py-1 text-xs text-zinc-200 outline-none"
                    value={localLabel}
                    onChange={(e) => setLocalLabel(e.target.value)}
                    onBlur={() => onRename(localLabel)}
                    onKeyDown={(e) => { if (e.key === 'Enter') onRename(localLabel); }}
                    placeholder="Label"
                />
                <select
                    className="w-full bg-zinc-800 border border-zinc-700 rounded px-2 py-1 text-xs text-zinc-200 outline-none"
                    value={targetId ?? '__end__'}
                    onChange={(e) => onChangeTarget(e.target.value === '__end__' ? null : e.target.value)}
                >
                    <option value="__end__">End Orchestration</option>
                    {otherSteps.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
                </select>
                <input
                    className="w-full bg-zinc-800 border border-zinc-700 rounded px-2 py-1 text-[10px] text-zinc-400 outline-none"
                    value={localDesc}
                    onChange={(e) => setLocalDesc(e.target.value)}
                    onBlur={() => onChangeDescription(localDesc)}
                    onKeyDown={(e) => { if (e.key === 'Enter') onChangeDescription(localDesc); }}
                    placeholder="When should this route be chosen? (helps LLM decide)"
                />
                <div className="text-right">
                    <button
                        onClick={onDelete}
                        className="w-full py-1 text-xs font-semibold text-red-400 bg-red-900/5 border border-red-700 rounded hover:bg-red-900/20 hover:text-red-300 transition-colors"
                        title="Delete this route"
                    >
                        <Trash2 size={12} className="inline-block mr-1" /> Delete Route
                    </button>
                </div>
            </div>
            
        </div>
    );
}
