'use client';
/* eslint-disable @typescript-eslint/no-explicit-any */
import { memo } from 'react';
import { Handle, Position } from '@xyflow/react';
import { Bot, Scale, GitBranch, GitMerge, RefreshCw, User, Code, Square, Zap, Wrench, Braces, GitFork, ArrowLeftRight, FileText } from 'lucide-react';
import { STEP_TYPE_META } from '@/types/orchestration';
import type { StepConfig, StepType } from '@/types/orchestration';

const ICONS: Record<string, React.FC<{ size?: number; className?: string }>> = {
    Bot, Scale, GitBranch, GitMerge, RefreshCw, User, Code, Square, Zap, Wrench, Braces, GitFork, ArrowLeftRight, FileText,
};

// Consistent color palette for evaluator routes (avoids red which implies error)
const ROUTE_COLORS = ['#10b981', '#3b82f6', '#f59e0b', '#8b5cf6', '#ec4899', '#06b6d4'];

function StepNodeComponent({ data, selected }: { data: any; selected?: boolean }) {
    const step: StepConfig = data.step;
    const isEntry: boolean = data.isEntry;
    const runStatus: string | undefined = data.runStatus;
    const agentName: string | undefined = data.agentName;
    const meta = STEP_TYPE_META[step.type as StepType];
    if (!meta) return null;
    const IconComp = ICONS[meta.icon] || Bot;

    const statusColors: Record<string, string> = {
        pending: 'border-zinc-600',
        running: 'border-blue-400 shadow-blue-400/30 shadow-lg animate-pulse',
        paused: 'border-amber-400 shadow-amber-400/30 shadow-lg',
        completed: 'border-green-500',
        failed: 'border-red-500',
    };

    const borderClass = runStatus ? statusColors[runStatus] || 'border-zinc-600' : selected ? 'border-blue-500' : 'border-zinc-600';
    const routeLabels = step.type === 'evaluator' ? Object.keys(step.route_map || {}) : [];
    const switchCaseLabels = step.type === 'switch' ? Object.keys(step.switch_cases || {}) : [];
    const isEnd = step.type === 'end';
    const isLoop = step.type === 'loop';
    const isEvaluator = step.type === 'evaluator';
    const isIfElse = step.type === 'if_else';
    const isSwitch = step.type === 'switch';

    return (
        <div
            className={`relative bg-zinc-800 rounded-lg border-2 ${borderClass} min-w-[160px] max-w-[220px] transition-all`}
            style={{ borderColor: selected ? meta.color : undefined }}
        >
            {/* Input handle — left side */}
            <Handle type="target" position={Position.Left} className="!bg-zinc-400 !w-3 !h-3 !border-2 !border-zinc-700" />

            {/* Header */}
            <div className="flex items-center gap-2 px-3 py-2 border-b border-zinc-700" style={{ backgroundColor: meta.color + '20' }}>
                <div style={{ color: meta.color }}><IconComp size={14} /></div>
                <span className="text-xs font-medium text-zinc-300 truncate">{step.name}</span>
                {isEntry && <span className="text-[10px] bg-green-600 text-white px-1.5 py-0.5 rounded-full ml-auto">START</span>}
            </div>

            {/* Body */}
            <div className="px-3 py-2 space-y-1">
                <div className="text-[10px] uppercase tracking-wider text-zinc-500">{meta.label}</div>

                {/* Agent — show agent name */}
                {step.type === 'agent' && agentName && (
                    <div className="text-xs text-zinc-400 truncate">{agentName}</div>
                )}

                {/* Tool — show forced tool name */}
                {step.type === 'tool' && (
                    <div className="text-[10px] text-purple-400 truncate">
                        {step.forced_tool ? step.forced_tool : 'No tool selected'}
                    </div>
                )}

                {/* LLM — show prompt snippet */}
                {step.type === 'llm' && (
                    <div className="text-[10px] text-teal-400 truncate">
                        {step.prompt_template ? step.prompt_template.slice(0, 40) + (step.prompt_template.length > 40 ? '…' : '') : 'No prompt set'}
                    </div>
                )}

                {/* Evaluator — show route count */}
                {isEvaluator && routeLabels.length > 0 && (
                    <div className="text-[10px] text-emerald-400">
                        {routeLabels.length} route{routeLabels.length !== 1 ? 's' : ''}: {routeLabels.join(', ')}
                    </div>
                )}

                {/* Parallel — show branch count */}
                {step.type === 'parallel' && (
                    <div className="text-[10px] text-purple-400">
                        {(step.parallel_branches || []).length} branch{(step.parallel_branches || []).length !== 1 ? 'es' : ''}
                    </div>
                )}

                {/* Merge — show strategy */}
                {step.type === 'merge' && (
                    <div className="text-[10px] text-pink-400">{step.merge_strategy || 'list'}</div>
                )}

                {/* Loop — show count */}
                {isLoop && (
                    <div className="text-[10px] text-amber-400">{step.loop_count || 3}× iterations</div>
                )}

                {/* Human */}
                {step.type === 'human' && (
                    <div className="text-[10px] text-red-400 truncate">{step.human_prompt || 'Awaiting input...'}</div>
                )}

                {/* End — minimal */}
                {isEnd && (
                    <div className="text-[10px] text-zinc-500">Terminates flow</div>
                )}

                {/* Extract JSON — show input info */}
                {step.type === 'extract_json' && (
                    <div className="text-[10px] text-orange-400 truncate">
                        {step.input_keys && step.input_keys.length > 0
                            ? `From: ${step.input_keys.join(', ')}`
                            : 'No input configured'}
                    </div>
                )}

                {/* Print — show content preview */}
                {step.type === 'print' && (
                    <div className="text-[10px] text-lime-400 truncate">
                        {step.print_content
                            ? step.print_content.slice(0, 40) + (step.print_content.length > 40 ? '…' : '')
                            : 'No content set'}
                    </div>
                )}

                {/* IF/Else — show condition */}
                {isIfElse && (
                    <div className="text-[10px] text-yellow-400 truncate">
                        {step.if_condition
                            ? step.if_condition.slice(0, 40) + (step.if_condition.length > 40 ? '…' : '')
                            : 'No condition set'}
                    </div>
                )}

                {/* Switch — show case count + expression */}
                {isSwitch && switchCaseLabels.length > 0 && (
                    <div className="text-[10px] text-cyan-400">
                        {switchCaseLabels.length} case{switchCaseLabels.length !== 1 ? 's' : ''}: {switchCaseLabels.join(', ')}
                    </div>
                )}
                {isSwitch && step.switch_expression && (
                    <div className="text-[10px] text-cyan-300/60 truncate">
                        {step.switch_expression.slice(0, 35) + (step.switch_expression.length > 35 ? '…' : '')}
                    </div>
                )}

                {/* Input keys */}
                {step.input_keys && step.input_keys.length > 0 && (
                    <div className="text-[10px] text-zinc-500">
                        <span className="text-zinc-600">in:</span> {step.input_keys.join(', ')}
                    </div>
                )}

                {/* Output key */}
                {step.output_key && (
                    <div className="text-[10px] text-zinc-500">
                        <span className="text-zinc-600">out:</span> {step.output_key}
                    </div>
                )}

                {/* Loop guard */}
                {step.max_iterations && step.max_iterations > 1 && step.max_iterations < 100 && (
                    <div className="text-[10px] text-purple-400">max {step.max_iterations}x</div>
                )}
            </div>

            {/* Output handles — right side */}
            {isEnd ? (
                // End node — no output handles
                null
            ) : isEvaluator && routeLabels.length > 0 ? (
                // Evaluator — one handle per route label, stacked vertically on right
                <>
                    {routeLabels.map((label, idx) => {
                        const total = routeLabels.length;
                        const offset = total === 1 ? 50 : 20 + (idx * 60) / (total - 1);
                        const color = ROUTE_COLORS[idx % ROUTE_COLORS.length];
                        return (
                            <Handle
                                key={`route_${label}`}
                                type="source"
                                position={Position.Right}
                                id={`route_${label}`}
                                className="!w-3 !h-3 !border-2 !border-zinc-700"
                                style={{ top: `${offset}%`, backgroundColor: color }}
                            />
                        );
                    })}
                    <div className="absolute -right-1 translate-x-full text-[9px] flex flex-col gap-0.5 pointer-events-none" style={{ top: '20%' }}>
                        {routeLabels.map((label, idx) => {
                            const color = ROUTE_COLORS[idx % ROUTE_COLORS.length];
                            return (
                                <span key={label} className="leading-tight" style={{ color }}>{label}</span>
                            );
                        })}
                    </div>
                </>
            ) : isLoop ? (
                // Loop — "body" (top-right, amber) and "done" (bottom-right, green) handles
                <>
                    <Handle
                        type="source"
                        position={Position.Right}
                        id="body"
                        className="!w-3 !h-3 !border-2 !border-zinc-700 !bg-amber-400"
                        style={{ top: '35%' }}
                    />
                    <Handle
                        type="source"
                        position={Position.Right}
                        id="done"
                        className="!w-3 !h-3 !border-2 !border-zinc-700 !bg-green-500"
                        style={{ top: '65%' }}
                    />
                    <div className="absolute -right-1 translate-x-full text-[9px] flex flex-col pointer-events-none" style={{ top: '25%' }}>
                        <span className="text-amber-400 leading-relaxed">body</span>
                        <span className="text-green-500 leading-relaxed">done</span>
                    </div>
                </>
            ) : isIfElse ? (
                // IF/Else — "true" (green, top) and "false" (red, bottom) handles
                <>
                    <Handle
                        type="source"
                        position={Position.Right}
                        id="if_true"
                        className="!w-3 !h-3 !border-2 !border-zinc-700 !bg-green-500"
                        style={{ top: '35%' }}
                    />
                    <Handle
                        type="source"
                        position={Position.Right}
                        id="if_false"
                        className="!w-3 !h-3 !border-2 !border-zinc-700 !bg-red-500"
                        style={{ top: '65%' }}
                    />
                    <div className="absolute -right-1 translate-x-full text-[9px] flex flex-col pointer-events-none" style={{ top: '25%' }}>
                        <span className="text-green-500 leading-relaxed">true</span>
                        <span className="text-red-500 leading-relaxed">false</span>
                    </div>
                </>
            ) : isSwitch && switchCaseLabels.length > 0 ? (
                // Switch — one handle per case + default, stacked vertically (like evaluator)
                <>
                    {switchCaseLabels.map((caseVal, idx) => {
                        const total = switchCaseLabels.length + 1; // +1 for default
                        const offset = total === 1 ? 50 : 15 + (idx * 70) / (total - 1);
                        const color = ROUTE_COLORS[idx % ROUTE_COLORS.length];
                        return (
                            <Handle
                                key={`case_${caseVal}`}
                                type="source"
                                position={Position.Right}
                                id={`case_${caseVal}`}
                                className="!w-3 !h-3 !border-2 !border-zinc-700"
                                style={{ top: `${offset}%`, backgroundColor: color }}
                            />
                        );
                    })}
                    {/* Default handle at the bottom */}
                    <Handle
                        type="source"
                        position={Position.Right}
                        id="default"
                        className="!w-3 !h-3 !border-2 !border-zinc-700 !bg-zinc-400"
                        style={{ top: `${15 + (switchCaseLabels.length * 70) / switchCaseLabels.length}%` }}
                    />
                    <div className="absolute -right-1 translate-x-full text-[9px] flex flex-col gap-0.5 pointer-events-none" style={{ top: '15%' }}>
                        {switchCaseLabels.map((caseVal, idx) => {
                            const color = ROUTE_COLORS[idx % ROUTE_COLORS.length];
                            return (
                                <span key={caseVal} className="leading-tight" style={{ color }}>{caseVal}</span>
                            );
                        })}
                        <span className="leading-tight text-zinc-400">default</span>
                    </div>
                </>
            ) : (
                // Default single output handle — right side
                <Handle type="source" position={Position.Right} className="!bg-zinc-400 !w-3 !h-3 !border-2 !border-zinc-700" />
            )}
        </div>
    );
}

export const StepNode = memo(StepNodeComponent);
