'use client';
/* eslint-disable @typescript-eslint/no-explicit-any */
/**
 * A workflow on the canvas: its steps as xyflow nodes, its links as edges, and
 * every edit the canvas can make, written back to the orchestration (M3a).
 *
 * Lifted out of `WorkflowCanvas` so the editor's open canvas
 * (`CanvasWorkbench`) and the classic canvas share one copy. The classic one
 * is still what cloud's editor, both "How I'm built" panels and cloud's
 * template preview draw, with the same props and the same behaviour.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
    useEdgesState,
    useNodesState,
    useReactFlow,
    MarkerType,
    type Connection,
    type Edge,
    type Node,
    type NodeTypes,
} from '@xyflow/react';
import type { CanvasFlags } from './canvasFlags';
import { StepNode } from './StepNode';
import { STEP_DRAG_TYPE } from './StepPalette';
import { addStepToGraph, clearEdgeLink, removeStepFromGraph, withInputKey, type EdgeLink } from './graph';
import { layoutPositions } from './layout';
import { validateOrchestration } from './validate';
import { agentKind } from './agentKind';
import type { Orchestration, StepConfig, StepIssue, StepNodeData, StepType } from '@/types/orchestration';
import type { StepTime } from '../run/model';

export type RunStepStatus = 'pending' | 'running' | 'paused' | 'completed' | 'failed';

const NO_ISSUES: Record<string, StepIssue[]> = {};

export const nodeTypes: NodeTypes = {
    stepNode: StepNode as any,
};

/** The steps the run on the canvas has a result for, and what opens one (F902). */
export type StepResults = { has: ReadonlySet<string>; open: (stepId: string) => void };

function stepsToNodes(
    steps: StepConfig[],
    entryStepId: string,
    agents: any[],
    selectedStepId: string | null,
    runStatuses: Record<string, string> | undefined,
    issuesByStep: Record<string, StepIssue[]>,
    runErrors?: Record<string, string>,
    runDetail?: boolean,
    stepTimes?: Record<string, StepTime> | null,
    settled?: boolean,
    workflowNames?: Record<string, string>,
    stepResults?: StepResults | null,
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
                workflowName: step.type === 'workflow' && step.workflow_id ? workflowNames?.[step.workflow_id] : undefined,
                agentKind: step.type === 'agent' && agent ? agentKind(agent) : undefined,
                runStatus: runStatuses?.[step.id] as StepNodeData['runStatus'],
                runError: runErrors?.[step.id],
                runDetail,
                lastRun: stepTimes?.[step.id],
                unreached: !!settled && stepTimes?.[step.id]?.state === 'not-reached',
                hasResult: !!stepResults?.has.has(step.id),
                openResult: stepResults?.open,
                issues: issuesByStep[step.id],
            },
            selected: step.id === selectedStepId,
            // A focused node is announced by its step's name, not as an unnamed group.
            ariaLabel: step.name || step.id,
        };
    });
}

/**
 * One builder for every edge on the canvas.
 *
 * Every connection is the same quiet line (D211): the word on its handle says
 * which road it is (true, false, a route's name, a case, done), "at limit" says
 * it on the line (its handle has no word), loops and parallel branches are
 * dashed, and colour on the canvas is kept for who does the work and for a run
 * in progress. `data` carries the relationship the edge
 * stands for, so deleting it can clear exactly the field that drew it — see
 * `clearEdgeLink`. Implicit `seq` edges (chains inside a loop body or parallel
 * branch) follow from list membership, not a link field, so they are drawn but
 * not deletable.
 */
function edge(opts: {
    id: string;
    source: string;
    target: string;
    link: EdgeLink;
    sourceHandle?: string;
    label?: string;
    dashed?: boolean;
    thin?: boolean;
}): Edge {
    const color = 'var(--flow-edge)';
    const implicit = opts.link.kind === 'seq';
    return {
        id: opts.id,
        source: opts.source,
        target: opts.target,
        sourceHandle: opts.sourceHandle,
        type: 'smoothstep',
        label: opts.label,
        data: opts.link,
        deletable: !implicit,
        selectable: !implicit,
        labelStyle: { fill: 'var(--text-muted)', fontSize: 11 },
        labelBgStyle: { fill: 'var(--surface)', fillOpacity: 0.9 },
        markerEnd: { type: MarkerType.ArrowClosed, width: opts.thin ? 14 : 16, height: opts.thin ? 14 : 16, color },
        style: {
            stroke: color,
            strokeWidth: opts.thin ? 1.25 : 1.6,
            ...(opts.dashed ? { strokeDasharray: '5,5' } : {}),
        },
    };
}

function stepsToEdges(steps: StepConfig[], editable = true): Edge[] {
    const edges: Edge[] = [];

    for (const step of steps) {
        // --- Any step: where a route to it goes once it has used its Max
        // iterations (F248). Drawn from its own `limit` handle, dashed: a road
        // taken instead of this step, not after it. ---
        if (step.on_exhausted_step_id) {
            edges.push(edge({
                id: `${step.id}->limit->${step.on_exhausted_step_id}`,
                source: step.id,
                sourceHandle: 'limit',
                target: step.on_exhausted_step_id,
                link: { kind: 'limit' },
                label: 'at limit',
                dashed: true,
            }));
        }

        // --- EVALUATOR: one edge per route_map entry ---
        if (step.type === 'evaluator' && step.route_map) {
            const labels = Object.keys(step.route_map);
            labels.forEach((label) => {
                const targetId = step.route_map![label];
                if (!targetId) return; // null = end orchestration, no edge to draw
                edges.push(edge({
                    id: `${step.id}->route_${label}->${targetId}`,
                    source: step.id,
                    sourceHandle: `route_${label}`,
                    target: targetId,
                    link: { kind: 'route', key: label },
                }));
            });
            // Evaluator may also have a next_step_id as fallback — skip if routes exist
            if (labels.length > 0) continue;
        }

        // --- IF/ELSE: true and false paths ---
        if (step.type === 'if_else') {
            if (step.if_true_step_id) {
                edges.push(edge({
                    id: `${step.id}->if_true->${step.if_true_step_id}`,
                    source: step.id,
                    sourceHandle: 'if_true',
                    target: step.if_true_step_id,
                    link: { kind: 'if_true' },
                }));
            }
            if (step.if_false_step_id) {
                edges.push(edge({
                    id: `${step.id}->if_false->${step.if_false_step_id}`,
                    source: step.id,
                    sourceHandle: 'if_false',
                    target: step.if_false_step_id,
                    link: { kind: 'if_false' },
                }));
            }
            continue;
        }

        // --- SWITCH: one edge per case + default ---
        if (step.type === 'switch' && step.switch_cases) {
            const caseKeys = Object.keys(step.switch_cases);
            caseKeys.forEach((caseVal) => {
                const targetId = step.switch_cases![caseVal];
                if (!targetId) return;
                edges.push(edge({
                    id: `${step.id}->case_${caseVal}->${targetId}`,
                    source: step.id,
                    sourceHandle: `case_${caseVal}`,
                    target: targetId,
                    link: { kind: 'case', key: caseVal },
                }));
            });
            if (step.switch_default_step_id) {
                edges.push(edge({
                    id: `${step.id}->default->${step.switch_default_step_id}`,
                    source: step.id,
                    sourceHandle: 'default',
                    target: step.switch_default_step_id,
                    link: { kind: 'switch_default' },
                }));
            }
            continue;
        }

        // --- LOOP: body handle → first body step, done handle → next_step_id ---
        if (step.type === 'loop') {
            const bodyIds = step.loop_step_ids || [];
            if (bodyIds.length > 0) {
                edges.push(edge({
                    id: `${step.id}->body->${bodyIds[0]}`,
                    source: step.id,
                    sourceHandle: 'body',
                    target: bodyIds[0],
                    link: { kind: 'loop_body' },
                    dashed: true,
                }));
                // Intra-body sequential edges — implicit, not deletable
                for (let i = 0; i < bodyIds.length - 1; i++) {
                    edges.push(edge({
                        id: `loop_body_${step.id}_${bodyIds[i]}->${bodyIds[i + 1]}`,
                        source: bodyIds[i],
                        target: bodyIds[i + 1],
                        link: { kind: 'seq' },
                        dashed: true,
                        thin: true,
                    }));
                }
            }
            if (step.next_step_id) {
                edges.push(edge({
                    id: `${step.id}->done->${step.next_step_id}`,
                    source: step.id,
                    sourceHandle: 'done',
                    target: step.next_step_id,
                    link: { kind: 'loop_done' },
                }));
            }
            continue;
        }

        // --- PARALLEL: edges to first step of each branch + intra-branch edges ---
        if (step.type === 'parallel' && step.parallel_branches) {
            for (const branch of step.parallel_branches) {
                if (branch.length === 0) continue;
                edges.push(edge({
                    id: `${step.id}->par->${branch[0]}`,
                    source: step.id,
                    target: branch[0],
                    link: { kind: 'parallel_entry' },
                    dashed: true,
                }));
                for (let i = 0; i < branch.length - 1; i++) {
                    edges.push(edge({
                        id: `par_${step.id}_${branch[i]}->${branch[i + 1]}`,
                        source: branch[i],
                        target: branch[i + 1],
                        link: { kind: 'seq' },
                        dashed: true,
                        thin: true,
                    }));
                }
            }
            if (step.next_step_id) {
                edges.push(edge({
                    id: `${step.id}->${step.next_step_id}`,
                    source: step.id,
                    target: step.next_step_id,
                    link: { kind: 'next' },
                }));
            }
            continue;
        }

        // --- DEFAULT: linear next_step_id edge ---
        if (step.next_step_id) {
            edges.push(edge({
                id: `${step.id}->${step.next_step_id}`,
                source: step.id,
                target: step.next_step_id,
                link: { kind: 'next' },
            }));
        }
    }

    return editable ? edges : edges.map((e) => ({ ...e, deletable: false, selectable: false }));
}

const noop = () => {};

export interface WorkflowGraphOptions {
    orchestration: Orchestration;
    agents: any[];
    /** Workflow id → name, for a Workflow step's card (E3); a host without the list shows none. */
    workflowNames?: Record<string, string>;
    selectedStepId: string | null;
    onSelectStep?: (stepId: string | null) => void;
    onUpdateOrchestration: (orch: Orchestration) => void;
    runStepStatuses?: Record<string, RunStepStatus>;
    runStepErrors?: Record<string, string>;
    /** The open canvas's steps say how the run stands in words too (Working, Needs you); the classic canvas's don't. */
    runDetail?: boolean;
    /** Last run: each step's state and time, shown on the step (the open canvas, M3a-2). */
    stepTimes?: Record<string, StepTime> | null;
    /** The run shown is over: a step it never reached is dimmed. */
    settled?: boolean;
    /** The steps the run on the canvas has a result for, and what opens one (F902). */
    stepResults?: StepResults | null;
    flags: CanvasFlags;
    /** Fit the graph in view. Called once per opened orchestration, and after Tidy up with a duration. */
    fit: (duration?: number) => void;
    /** The element holding the `<ReactFlow>`: Delete and Backspace act only while the person is working in it. */
    wrapperRef: React.RefObject<HTMLElement | null>;
}

export function useWorkflowGraph({
    orchestration,
    agents,
    workflowNames,
    selectedStepId,
    onSelectStep,
    onUpdateOrchestration,
    runStepStatuses,
    runStepErrors,
    runDetail,
    stepTimes,
    settled,
    stepResults,
    flags,
    fit,
    wrapperRef,
}: WorkflowGraphOptions) {
    const { getNodes, screenToFlowPosition } = useReactFlow();
    const selectStep = onSelectStep ?? noop;

    // A preview has no draft to fix, and its agents may be local ids the
    // validator would call missing.
    const issuesByStep = useMemo(
        () => (flags.issues ? validateOrchestration(orchestration, agents).byStep : NO_ISSUES),
        [flags.issues, orchestration, agents]
    );

    const computedNodes = useMemo(
        () => stepsToNodes(orchestration.steps, orchestration.entry_step_id, agents, selectedStepId, runStepStatuses, issuesByStep, runStepErrors, runDetail, stepTimes, settled, workflowNames, stepResults),
        [orchestration.steps, orchestration.entry_step_id, agents, selectedStepId, runStepStatuses, issuesByStep, runStepErrors, runDetail, stepTimes, settled, workflowNames, stepResults]
    );
    const computedEdges = useMemo(
        () => stepsToEdges(orchestration.steps, flags.edits),
        [orchestration.steps, flags.edits]
    );

    const [nodes, setNodes, onNodesChange] = useNodesState(computedNodes);
    const [edges, setEdges, onEdgesChange] = useEdgesState(computedEdges);

    // xyflow listens for Delete and Backspace on the whole document and skips
    // only text fields and `.nokey`, so Backspace on any focused button — the
    // panel's "Set as entry point", a dialog's button, which Radix puts outside
    // the panel — deleted the selected step. The keys now act only while the
    // person's last click or focus was on the canvas itself (M3a).
    const [keysOnCanvas, setKeysOnCanvas] = useState(true);
    useEffect(() => {
        const note = (event: Event) => {
            const target = event.target;
            setKeysOnCanvas(target instanceof window.Node && !!wrapperRef.current?.contains(target));
        };
        document.addEventListener('pointerdown', note, true);
        document.addEventListener('focusin', note, true);
        return () => {
            document.removeEventListener('pointerdown', note, true);
            document.removeEventListener('focusin', note, true);
        };
    }, [wrapperRef]);
    const deleteKeyCode = keysOnCanvas ? flags.deleteKeyCode : null;

    // The orchestration is the source of truth; React Flow's copy exists for
    // drag interactions. Re-derive whenever the source changes — keeping each
    // step's measured size: xyflow re-measures a node that arrives without one,
    // and until it has, it draws none of that node's connections, so every
    // change of a run's step status blanked every connection for a frame (and
    // took the live run's signals with it, M3a-2). A real change of size is still
    // measured: xyflow watches each step's element.
    useEffect(() => {
        setNodes((current) => {
            const measured = new Map(current.map((n) => [n.id, n.measured]));
            return computedNodes.map((n) => {
                const size = measured.get(n.id);
                return size ? { ...n, measured: size } : n;
            });
        });
    }, [computedNodes, setNodes]);
    useEffect(() => { setEdges(computedEdges); }, [computedEdges, setEdges]);

    // Refit when switching to a different orchestration — not on every edit.
    const fittedForRef = useRef<string | null>(null);
    const fitRef = useRef(fit);
    useEffect(() => { fitRef.current = fit; });
    useEffect(() => {
        if (fittedForRef.current === orchestration.id) return;
        fittedForRef.current = orchestration.id;
        window.requestAnimationFrame(() => fitRef.current());
    }, [orchestration.id]);

    const onConnect = useCallback(
        (connection: Connection) => {
            const sourceStep = orchestration.steps.find((s) => s.id === connection.source);
            if (!sourceStep || !connection.target) return;

            const updatedSteps = orchestration.steps.map((s) => {
                // --- SOURCE step: update routing fields ---
                if (s.id === connection.source) {
                    if (connection.sourceHandle === 'limit') {
                        return { ...s, on_exhausted_step_id: connection.target! };
                    }
                    if (s.type === 'evaluator' && connection.sourceHandle?.startsWith('route_')) {
                        const label = connection.sourceHandle.replace('route_', '');
                        return { ...s, route_map: { ...(s.route_map || {}), [label]: connection.target! } };
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
                    if (s.type === 'if_else') {
                        if (connection.sourceHandle === 'if_true') {
                            return { ...s, if_true_step_id: connection.target! };
                        }
                        if (connection.sourceHandle === 'if_false') {
                            return { ...s, if_false_step_id: connection.target! };
                        }
                    }
                    if (s.type === 'switch') {
                        if (connection.sourceHandle?.startsWith('case_')) {
                            const caseVal = connection.sourceHandle.replace('case_', '');
                            return { ...s, switch_cases: { ...(s.switch_cases || {}), [caseVal]: connection.target! } };
                        }
                        if (connection.sourceHandle === 'default') {
                            return { ...s, switch_default_step_id: connection.target! };
                        }
                    }
                    return { ...s, next_step_id: connection.target! };
                }

                // --- TARGET step: auto-append source's output_key to input_keys ---
                if (s.id === connection.target) return withInputKey(s, sourceStep.output_key);

                return s;
            });
            onUpdateOrchestration({ ...orchestration, steps: updatedSteps });
        },
        [orchestration, onUpdateOrchestration]
    );

    const onNodeDragStop = useCallback(
        (_: any, node: Node, draggedNodes?: Node[]) => {
            const moved = new Map((draggedNodes?.length ? draggedNodes : [node]).map((n) => [n.id, n.position]));
            const updatedSteps = orchestration.steps.map((s) => {
                const pos = moved.get(s.id);
                return pos ? { ...s, position_x: pos.x, position_y: pos.y } : s;
            });
            onUpdateOrchestration({ ...orchestration, steps: updatedSteps });
        },
        [orchestration, onUpdateOrchestration]
    );

    // Delete key and edge deletion both resolve to the shared graph mutations.
    const onDelete = useCallback(
        ({ nodes: deletedNodes, edges: deletedEdges }: { nodes: Node[]; edges: Edge[] }) => {
            let next = orchestration;
            const deletedIds = new Set(deletedNodes.map((n) => n.id));
            for (const n of deletedNodes) next = removeStepFromGraph(next, n.id);
            for (const e of deletedEdges) {
                // Edges to or from a deleted node are already cleaned by removeStepFromGraph.
                if (deletedIds.has(e.source) || deletedIds.has(e.target)) continue;
                if (!e.data?.kind) continue;
                next = clearEdgeLink(next, e.source, e.target, e.data as unknown as EdgeLink);
            }
            if (next !== orchestration) {
                onUpdateOrchestration(next);
                if (selectedStepId && deletedIds.has(selectedStepId)) selectStep(null);
            }
        },
        [orchestration, onUpdateOrchestration, selectedStepId, selectStep]
    );

    const addStep = useCallback(
        (type: StepType, position?: { x: number; y: number }) => {
            const { orchestration: next, step } = addStepToGraph(orchestration, type, position, selectedStepId);
            onUpdateOrchestration(next);
            selectStep(step.id);
            return step;
        },
        [orchestration, onUpdateOrchestration, selectStep, selectedStepId]
    );

    const onDragOver = useCallback((event: React.DragEvent) => {
        if (event.dataTransfer.types.includes(STEP_DRAG_TYPE)) {
            event.preventDefault();
            event.dataTransfer.dropEffect = 'move';
        }
    }, []);

    const onDrop = useCallback(
        (event: React.DragEvent) => {
            const type = event.dataTransfer.getData(STEP_DRAG_TYPE) as StepType;
            if (!type) return;
            event.preventDefault();
            const position = screenToFlowPosition({ x: event.clientX, y: event.clientY });
            // Center the node on the cursor rather than hanging it off the corner.
            addStep(type, { x: Math.round(position.x - 100), y: Math.round(position.y - 40) });
        },
        [screenToFlowPosition, addStep]
    );

    const tidyUp = useCallback(() => {
        const positioned = layoutPositions(getNodes(), computedEdges);
        const updatedSteps = orchestration.steps.map((s) => {
            const pos = positioned[s.id];
            return pos ? { ...s, position_x: pos.x, position_y: pos.y } : s;
        });
        onUpdateOrchestration({ ...orchestration, steps: updatedSteps });
        window.requestAnimationFrame(() => fitRef.current(300));
    }, [getNodes, computedEdges, orchestration, onUpdateOrchestration]);

    const onNodeClick = useCallback(
        (_: any, node: Node) => {
            selectStep(node.id);
        },
        [selectStep]
    );

    const onPaneClick = useCallback(() => {
        selectStep(null);
    }, [selectStep]);

    // xyflow selects a focused node on Enter or Space, but calls `onNodeClick`
    // only for a mouse click, so a keyboard could select a step and never open
    // its settings (F219). The same keys open them here; Escape closes them, as a
    // click on the pane does. Selection on mouse-down is left alone, so dragging a
    // node still does not open anything.
    const onCanvasKeyDown = useCallback((e: React.KeyboardEvent<HTMLDivElement>) => {
        if (e.key !== 'Enter' && e.key !== ' ' && e.key !== 'Escape') return;
        const node = (e.target as HTMLElement).closest?.('.react-flow__node');
        const id = node?.getAttribute('data-id');
        if (!id || node !== e.target) return;
        selectStep(e.key === 'Escape' ? null : id);
    }, [selectStep]);

    return {
        nodes,
        edges,
        onNodesChange,
        onEdgesChange,
        onConnect,
        onNodeDragStop,
        onDelete,
        onDragOver,
        onDrop,
        addStep,
        tidyUp,
        onNodeClick,
        onPaneClick,
        onCanvasKeyDown,
        issuesByStep,
        deleteKeyCode,
    };
}
