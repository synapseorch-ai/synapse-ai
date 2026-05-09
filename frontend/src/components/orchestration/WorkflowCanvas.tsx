'use client';
/* eslint-disable @typescript-eslint/no-explicit-any */
import { useCallback, useEffect, useMemo } from 'react';
import {
    ReactFlow,
    Background,
    Controls,
    MiniMap,
    addEdge,
    useNodesState,
    useEdgesState,
    type Connection,
    type Edge,
    type Node,
    type NodeTypes,
    BackgroundVariant,
    MarkerType,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import { StepNode } from './StepNode';
import { STEP_TYPE_META } from '@/types/orchestration';
import type { Orchestration, StepConfig, StepNodeData } from '@/types/orchestration';

interface WorkflowCanvasProps {
    orchestration: Orchestration;
    agents: any[];
    selectedStepId: string | null;
    onSelectStep: (stepId: string | null) => void;
    onUpdateOrchestration: (orch: Orchestration) => void;
    runStepStatuses?: Record<string, 'pending' | 'running' | 'paused' | 'completed' | 'failed'>;
}

const nodeTypes: NodeTypes = {
    stepNode: StepNode as any,
};


function stepsToNodes(
    steps: StepConfig[],
    entryStepId: string,
    agents: any[],
    selectedStepId: string | null,
    runStatuses?: Record<string, string>,
): Node<StepNodeData>[] {
    return steps.map((step) => {
        const agent = agents.find((a: any) => a.id === step.agent_id);
        return {
            id: step.id,
            type: 'stepNode',
            position: { x: step.position_x ?? 0, y: step.position_y ?? 0 },
            data: {
                step,
                isEntry: step.id === entryStepId,
                isSelected: step.id === selectedStepId,
                agentName: agent?.name,
                runStatus: runStatuses?.[step.id] as any,
            },
            selected: step.id === selectedStepId,
        };
    });
}

// Consistent color palette for evaluator routes (no red — avoids "error" association)
const ROUTE_COLORS = ['#10b981', '#3b82f6', '#f59e0b', '#8b5cf6', '#ec4899', '#06b6d4'];

function stepsToEdges(steps: StepConfig[]): Edge[] {
    const edges: Edge[] = [];
    const stepMap = new Map(steps.map(s => [s.id, s]));

    for (const step of steps) {
        // --- EVALUATOR: one edge per route_map entry ---
        if (step.type === 'evaluator' && step.route_map) {
            const labels = Object.keys(step.route_map);
            for (const label of labels) {
                const targetId = step.route_map[label];
                if (!targetId) continue; // null = end orchestration, no edge to draw
                const idx = labels.indexOf(label);
                const color = ROUTE_COLORS[idx % ROUTE_COLORS.length];
                edges.push({
                    id: `${step.id}->route_${label}->${targetId}`,
                    source: step.id,
                    sourceHandle: `route_${label}`,
                    target: targetId,
                    type: 'smoothstep',
                    label,
                    labelStyle: { fill: color, fontSize: 10 },
                    markerEnd: { type: MarkerType.ArrowClosed, width: 16, height: 16, color },
                    style: { stroke: color, strokeWidth: 2 },
                });
            }
            // Evaluator may also have a next_step_id as fallback — skip if routes exist
            if (labels.length > 0) continue;
        }

        // --- IF/ELSE: true edge (green) and false edge (red) ---
        if (step.type === 'if_else') {
            if (step.if_true_step_id) {
                edges.push({
                    id: `${step.id}->if_true->${step.if_true_step_id}`,
                    source: step.id,
                    sourceHandle: 'if_true',
                    target: step.if_true_step_id,
                    type: 'smoothstep',
                    label: 'true',
                    labelStyle: { fill: '#22c55e', fontSize: 10 },
                    markerEnd: { type: MarkerType.ArrowClosed, width: 16, height: 16, color: '#22c55e' },
                    style: { stroke: '#22c55e', strokeWidth: 2 },
                });
            }
            if (step.if_false_step_id) {
                edges.push({
                    id: `${step.id}->if_false->${step.if_false_step_id}`,
                    source: step.id,
                    sourceHandle: 'if_false',
                    target: step.if_false_step_id,
                    type: 'smoothstep',
                    label: 'false',
                    labelStyle: { fill: '#ef4444', fontSize: 10 },
                    markerEnd: { type: MarkerType.ArrowClosed, width: 16, height: 16, color: '#ef4444' },
                    style: { stroke: '#ef4444', strokeWidth: 2 },
                });
            }
            continue;
        }

        // --- SWITCH: one edge per case + default ---
        if (step.type === 'switch' && step.switch_cases) {
            const caseKeys = Object.keys(step.switch_cases);
            for (const caseVal of caseKeys) {
                const targetId = step.switch_cases[caseVal];
                if (!targetId) continue;
                const idx = caseKeys.indexOf(caseVal);
                const color = ROUTE_COLORS[idx % ROUTE_COLORS.length];
                edges.push({
                    id: `${step.id}->case_${caseVal}->${targetId}`,
                    source: step.id,
                    sourceHandle: `case_${caseVal}`,
                    target: targetId,
                    type: 'smoothstep',
                    label: caseVal,
                    labelStyle: { fill: color, fontSize: 10 },
                    markerEnd: { type: MarkerType.ArrowClosed, width: 16, height: 16, color },
                    style: { stroke: color, strokeWidth: 2 },
                });
            }
            if (step.switch_default_step_id) {
                edges.push({
                    id: `${step.id}->default->${step.switch_default_step_id}`,
                    source: step.id,
                    sourceHandle: 'default',
                    target: step.switch_default_step_id,
                    type: 'smoothstep',
                    label: 'default',
                    labelStyle: { fill: '#9ca3af', fontSize: 10 },
                    markerEnd: { type: MarkerType.ArrowClosed, width: 16, height: 16, color: '#9ca3af' },
                    style: { stroke: '#9ca3af', strokeWidth: 2 },
                });
            }
            continue;
        }

        // --- LOOP: body handle → first body step, done handle → next_step_id ---
        if (step.type === 'loop') {
            const bodyIds = step.loop_step_ids || [];
            if (bodyIds.length > 0) {
                // Body handle → first body step
                edges.push({
                    id: `${step.id}->body->${bodyIds[0]}`,
                    source: step.id,
                    sourceHandle: 'body',
                    target: bodyIds[0],
                    type: 'smoothstep',
                    style: { stroke: '#f59e0b', strokeWidth: 2, strokeDasharray: '5,5' },
                    markerEnd: { type: MarkerType.ArrowClosed, width: 16, height: 16, color: '#f59e0b' },
                });
                // Intra-body sequential edges
                for (let i = 0; i < bodyIds.length - 1; i++) {
                    edges.push({
                        id: `loop_body_${step.id}_${bodyIds[i]}->${bodyIds[i + 1]}`,
                        source: bodyIds[i],
                        target: bodyIds[i + 1],
                        type: 'smoothstep',
                        style: { stroke: '#f59e0b', strokeWidth: 1.5, strokeDasharray: '4,4' },
                        markerEnd: { type: MarkerType.ArrowClosed, width: 14, height: 14, color: '#f59e0b' },
                    });
                }
            }
            // Done path
            if (step.next_step_id) {
                edges.push({
                    id: `${step.id}->done->${step.next_step_id}`,
                    source: step.id,
                    sourceHandle: 'done',
                    target: step.next_step_id,
                    type: 'smoothstep',
                    label: 'done',
                    labelStyle: { fill: '#22c55e', fontSize: 10 },
                    markerEnd: { type: MarkerType.ArrowClosed, width: 16, height: 16, color: '#22c55e' },
                    style: { stroke: '#22c55e', strokeWidth: 2 },
                });
            }
            continue;
        }

        // --- PARALLEL: edges to first step of each branch + intra-branch edges ---
        if (step.type === 'parallel' && step.parallel_branches) {
            for (const branch of step.parallel_branches) {
                if (branch.length === 0) continue;
                // Edge from parallel node to first step of branch
                edges.push({
                    id: `${step.id}->par->${branch[0]}`,
                    source: step.id,
                    target: branch[0],
                    type: 'smoothstep',
                    style: { stroke: '#8b5cf6', strokeWidth: 2, strokeDasharray: '5,5' },
                    markerEnd: { type: MarkerType.ArrowClosed, width: 16, height: 16, color: '#8b5cf6' },
                });
                // Intra-branch sequential edges
                for (let i = 0; i < branch.length - 1; i++) {
                    edges.push({
                        id: `par_${step.id}_${branch[i]}->${branch[i + 1]}`,
                        source: branch[i],
                        target: branch[i + 1],
                        type: 'smoothstep',
                        style: { stroke: '#8b5cf6', strokeWidth: 1.5, strokeDasharray: '4,4' },
                        markerEnd: { type: MarkerType.ArrowClosed, width: 14, height: 14, color: '#8b5cf6' },
                    });
                }
            }
            // Parallel's next_step_id (e.g. to a merge node) as a regular edge
            if (step.next_step_id) {
                edges.push({
                    id: `${step.id}->${step.next_step_id}`,
                    source: step.id,
                    target: step.next_step_id,
                    type: 'smoothstep',
                    markerEnd: { type: MarkerType.ArrowClosed, width: 16, height: 16, color: '#6b7280' },
                    style: { stroke: '#6b7280', strokeWidth: 2 },
                });
            }
            continue;
        }

        // --- DEFAULT: linear next_step_id edge ---
        if (step.next_step_id) {
            edges.push({
                id: `${step.id}->${step.next_step_id}`,
                source: step.id,
                target: step.next_step_id,
                type: 'smoothstep',
                markerEnd: { type: MarkerType.ArrowClosed, width: 16, height: 16, color: '#6b7280' },
                style: { stroke: '#6b7280', strokeWidth: 2 },
            });
        }
    }

    return edges;
}

export function WorkflowCanvas({
    orchestration,
    agents,
    selectedStepId,
    onSelectStep,
    onUpdateOrchestration,
    runStepStatuses,
}: WorkflowCanvasProps) {
    const initialNodes = useMemo(
        () => stepsToNodes(orchestration.steps, orchestration.entry_step_id, agents, selectedStepId, runStepStatuses),
        [orchestration.steps, orchestration.entry_step_id, agents, selectedStepId, runStepStatuses]
    );
    const initialEdges = useMemo(
        () => stepsToEdges(orchestration.steps),
        [orchestration.steps]
    );

    const [nodes, setNodes, onNodesChange] = useNodesState(initialNodes);
    const [edges, setEdges, onEdgesChange] = useEdgesState(initialEdges);

    // Sync nodes when orchestration or run statuses change
    useEffect(() => {
        setNodes(stepsToNodes(orchestration.steps, orchestration.entry_step_id, agents, selectedStepId, runStepStatuses));
        setEdges(stepsToEdges(orchestration.steps));
    }, [orchestration, agents, selectedStepId, runStepStatuses, setNodes, setEdges]);

    const onConnect = useCallback(
        (connection: Connection) => {
            setEdges((eds) => addEdge({
                ...connection,
                type: 'smoothstep',
                markerEnd: { type: MarkerType.ArrowClosed, width: 16, height: 16, color: '#6b7280' },
                style: { stroke: '#6b7280', strokeWidth: 2 },
            }, eds));

            const sourceStep = orchestration.steps.find((s) => s.id === connection.source);
            if (!sourceStep || !connection.target) return;

            const updatedSteps = orchestration.steps.map((s) => {
                // --- SOURCE step: update routing fields ---
                if (s.id === connection.source) {
                    if (s.type === 'evaluator' && connection.sourceHandle?.startsWith('route_')) {
                        const label = connection.sourceHandle.replace('route_', '');
                        const newRouteMap = { ...(s.route_map || {}), [label]: connection.target! };
                        return { ...s, route_map: newRouteMap };
                    }
                    if (s.type === 'loop') {
                        if (connection.sourceHandle === 'body') {
                            const bodyIds = [...(s.loop_step_ids || [])];
                            if (!bodyIds.includes(connection.target!)) {
                                bodyIds.push(connection.target!);
                            }
                            return { ...s, loop_step_ids: bodyIds };
                        }
                        if (connection.sourceHandle === 'done') {
                            return { ...s, next_step_id: connection.target! };
                        }
                    }
                    // IF/ELSE handles
                    if (s.type === 'if_else') {
                        if (connection.sourceHandle === 'if_true') {
                            return { ...s, if_true_step_id: connection.target! };
                        }
                        if (connection.sourceHandle === 'if_false') {
                            return { ...s, if_false_step_id: connection.target! };
                        }
                    }
                    // SWITCH handles
                    if (s.type === 'switch') {
                        if (connection.sourceHandle?.startsWith('case_')) {
                            const caseVal = connection.sourceHandle.replace('case_', '');
                            const newCases = { ...(s.switch_cases || {}), [caseVal]: connection.target! };
                            return { ...s, switch_cases: newCases };
                        }
                        if (connection.sourceHandle === 'default') {
                            return { ...s, switch_default_step_id: connection.target! };
                        }
                    }
                    return { ...s, next_step_id: connection.target! };
                }

                // --- TARGET step: auto-append source's output_key to input_keys ---
                if (s.id === connection.target && sourceStep.output_key) {
                    const existingKeys = s.input_keys || [];
                    if (!existingKeys.includes(sourceStep.output_key)) {
                        return { ...s, input_keys: [...existingKeys, sourceStep.output_key] };
                    }
                }

                return s;
            });
            onUpdateOrchestration({ ...orchestration, steps: updatedSteps });
        },
        [orchestration, onUpdateOrchestration, setEdges]
    );

    const onNodeDragStop = useCallback(
        (_: any, node: Node) => {
            const updatedSteps = orchestration.steps.map((s) =>
                s.id === node.id ? { ...s, position_x: node.position.x, position_y: node.position.y } : s
            );
            onUpdateOrchestration({ ...orchestration, steps: updatedSteps });
        },
        [orchestration, onUpdateOrchestration]
    );

    const onNodeClick = useCallback(
        (_: any, node: Node) => {
            onSelectStep(node.id);
        },
        [onSelectStep]
    );

    const onPaneClick = useCallback(() => {
        onSelectStep(null);
    }, [onSelectStep]);

    return (
        <div className="w-full h-full">
            <ReactFlow
                nodes={nodes}
                edges={edges}
                onNodesChange={onNodesChange}
                onEdgesChange={onEdgesChange}
                onConnect={onConnect}
                onNodeDragStop={onNodeDragStop}
                onNodeClick={onNodeClick}
                onPaneClick={onPaneClick}
                nodeTypes={nodeTypes}
                fitView
                proOptions={{ hideAttribution: true }}
                className="bg-zinc-950"
                defaultEdgeOptions={{ type: 'smoothstep' }}
            >
                <Background variant={BackgroundVariant.Dots} gap={20} size={1} className="!bg-zinc-950" />
                <Controls className="!bg-zinc-800 !border-zinc-700 !shadow-lg [&>button]:!bg-zinc-700 [&>button]:!border-zinc-600 [&>button]:!text-zinc-200 [&>button:hover]:!bg-zinc-600" />
                <MiniMap
                    className="!bg-zinc-800 !border-zinc-700"
                    nodeColor={(node: any) => {
                        const type = node.data?.step?.type;
                        return STEP_TYPE_META[type as keyof typeof STEP_TYPE_META]?.color || '#6b7280';
                    }}
                    maskColor="rgba(0,0,0,0.6)"
                />
            </ReactFlow>
        </div>
    );
}
