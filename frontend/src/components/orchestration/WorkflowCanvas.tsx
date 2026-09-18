'use client';
/* eslint-disable @typescript-eslint/no-explicit-any */
import { useCallback, useEffect, useMemo, useRef, useSyncExternalStore } from 'react';
import {
    ReactFlow,
    Background,
    MiniMap,
    Panel,
    useNodesState,
    useEdgesState,
    useReactFlow,
    type Connection,
    type Edge,
    type Node,
    type NodeTypes,
    BackgroundVariant,
    MarkerType,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import { Maximize, Minus, Plus, Wand2 } from 'lucide-react';
import { canvasFlags } from './canvasFlags';
import { StepNode } from './StepNode';
import { PALETTE_CLEARANCE_PX, StepPalette, STEP_DRAG_TYPE, usePaletteCollapsed } from './StepPalette';
import { addStepToGraph, clearEdgeLink, removeStepFromGraph, ROUTE_COLORS, type EdgeLink } from './graph';
import { layoutPositions } from './layout';
import { validateOrchestration } from './validate';
import { STEP_TYPE_META } from '@/types/orchestration';
import type { Orchestration, StepConfig, StepIssue, StepNodeData, StepType } from '@/types/orchestration';

interface WorkflowCanvasProps {
    orchestration: Orchestration;
    agents: any[];
    selectedStepId?: string | null;
    /** Omit to make steps inert to clicks — a preview with nothing to open. */
    onSelectStep?: (stepId: string | null) => void;
    /** Required unless `readOnly`: every edit on the canvas arrives here. */
    onUpdateOrchestration?: (orch: Orchestration) => void;
    runStepStatuses?: Record<string, 'pending' | 'running' | 'paused' | 'completed' | 'failed'>;
    /** The palette rail is part of the canvas; a shell that renders its own can turn it off. */
    showPalette?: boolean;
    /** Look, don't touch: no drag, connect, delete, drop, palette, Tidy up or issue badges. See `canvasFlags`. */
    readOnly?: boolean;
}

const noop = () => {};

/** `(pointer: coarse)`, live — a tablet with a mouse plugged in changes its answer. The server render says no. */
const COARSE = '(pointer: coarse)';
function subscribeCoarse(onChange: () => void) {
    const media = window.matchMedia(COARSE);
    media.addEventListener('change', onChange);
    return () => media.removeEventListener('change', onChange);
}
const readCoarse = () => window.matchMedia(COARSE).matches;
const serverCoarse = () => false;
const NO_ISSUES: Record<string, StepIssue[]> = {};
/** Fit-view's air on each side, as a fraction of the canvas. */
const FIT_PADDING = 0.15;

const nodeTypes: NodeTypes = {
    stepNode: StepNode as any,
};

function stepsToNodes(
    steps: StepConfig[],
    entryStepId: string,
    agents: any[],
    selectedStepId: string | null,
    runStatuses: Record<string, string> | undefined,
    issuesByStep: Record<string, StepIssue[]>,
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
                issues: issuesByStep[step.id],
            },
            selected: step.id === selectedStepId,
        };
    });
}

/**
 * One builder for every edge on the canvas.
 *
 * Colors are CSS variables (declared in `globals.css` for both themes) except
 * the positional route/case colors, which are identity rather than semantics.
 * `data` carries the relationship the edge stands for, so deleting it can
 * clear exactly the field that drew it — see `clearEdgeLink`. Implicit `seq`
 * edges (chains inside a loop body or parallel branch) follow from list
 * membership, not a link field, so they are drawn but not deletable.
 */
function edge(opts: {
    id: string;
    source: string;
    target: string;
    link: EdgeLink;
    sourceHandle?: string;
    label?: string;
    color?: string;
    dashed?: boolean;
    thin?: boolean;
}): Edge {
    const color = opts.color ?? 'var(--flow-edge)';
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
        labelStyle: { fill: color, fontSize: 10 },
        labelBgStyle: { fill: 'var(--surface)', fillOpacity: 0.85 },
        markerEnd: { type: MarkerType.ArrowClosed, width: opts.thin ? 14 : 16, height: opts.thin ? 14 : 16, color },
        style: {
            stroke: color,
            strokeWidth: opts.thin ? 1.5 : 2,
            ...(opts.dashed ? { strokeDasharray: '5,5' } : {}),
        },
    };
}

function stepsToEdges(steps: StepConfig[], editable = true): Edge[] {
    const edges: Edge[] = [];

    for (const step of steps) {
        // --- EVALUATOR: one edge per route_map entry ---
        if (step.type === 'evaluator' && step.route_map) {
            const labels = Object.keys(step.route_map);
            labels.forEach((label, idx) => {
                const targetId = step.route_map![label];
                if (!targetId) return; // null = end orchestration, no edge to draw
                edges.push(edge({
                    id: `${step.id}->route_${label}->${targetId}`,
                    source: step.id,
                    sourceHandle: `route_${label}`,
                    target: targetId,
                    link: { kind: 'route', key: label },
                    label,
                    color: ROUTE_COLORS[idx % ROUTE_COLORS.length],
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
                    label: 'true',
                    color: 'var(--success)',
                }));
            }
            if (step.if_false_step_id) {
                edges.push(edge({
                    id: `${step.id}->if_false->${step.if_false_step_id}`,
                    source: step.id,
                    sourceHandle: 'if_false',
                    target: step.if_false_step_id,
                    link: { kind: 'if_false' },
                    label: 'false',
                    color: 'var(--danger)',
                }));
            }
            continue;
        }

        // --- SWITCH: one edge per case + default ---
        if (step.type === 'switch' && step.switch_cases) {
            const caseKeys = Object.keys(step.switch_cases);
            caseKeys.forEach((caseVal, idx) => {
                const targetId = step.switch_cases![caseVal];
                if (!targetId) return;
                edges.push(edge({
                    id: `${step.id}->case_${caseVal}->${targetId}`,
                    source: step.id,
                    sourceHandle: `case_${caseVal}`,
                    target: targetId,
                    link: { kind: 'case', key: caseVal },
                    label: caseVal,
                    color: ROUTE_COLORS[idx % ROUTE_COLORS.length],
                }));
            });
            if (step.switch_default_step_id) {
                edges.push(edge({
                    id: `${step.id}->default->${step.switch_default_step_id}`,
                    source: step.id,
                    sourceHandle: 'default',
                    target: step.switch_default_step_id,
                    link: { kind: 'switch_default' },
                    label: 'default',
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
                    color: 'var(--warning)',
                    dashed: true,
                }));
                // Intra-body sequential edges — implicit, not deletable
                for (let i = 0; i < bodyIds.length - 1; i++) {
                    edges.push(edge({
                        id: `loop_body_${step.id}_${bodyIds[i]}->${bodyIds[i + 1]}`,
                        source: bodyIds[i],
                        target: bodyIds[i + 1],
                        link: { kind: 'seq' },
                        color: 'var(--warning)',
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
                    label: 'done',
                    color: 'var(--success)',
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
                    color: 'var(--flow-parallel)',
                    dashed: true,
                }));
                for (let i = 0; i < branch.length - 1; i++) {
                    edges.push(edge({
                        id: `par_${step.id}_${branch[i]}->${branch[i + 1]}`,
                        source: branch[i],
                        target: branch[i + 1],
                        link: { kind: 'seq' },
                        color: 'var(--flow-parallel)',
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

export function WorkflowCanvas({
    orchestration,
    agents,
    selectedStepId = null,
    onSelectStep,
    onUpdateOrchestration = noop,
    runStepStatuses,
    showPalette = true,
    readOnly = false,
}: WorkflowCanvasProps) {
    const { fitView, getNodes, screenToFlowPosition, zoomIn, zoomOut } = useReactFlow();
    const coarsePointer = useSyncExternalStore(subscribeCoarse, readCoarse, serverCoarse);
    const flags = canvasFlags({ readOnly, showPalette, coarsePointer });
    const selectStep = onSelectStep ?? noop;
    const [paletteCollapsed] = usePaletteCollapsed();
    // The open palette floats over the canvas's left edge; every fit leaves it
    // that much room, or the first step opens underneath it (F31).
    const fitPadding = useMemo(
        () =>
            flags.palette && paletteCollapsed !== '1'
                ? { top: FIT_PADDING, right: FIT_PADDING, bottom: FIT_PADDING, left: `${PALETTE_CLEARANCE_PX}px` as const }
                : FIT_PADDING,
        [flags.palette, paletteCollapsed]
    );

    // A preview has no draft to fix, and its agents may be local ids the
    // validator would call missing.
    const issuesByStep = useMemo(
        () => (flags.issues ? validateOrchestration(orchestration, agents).byStep : NO_ISSUES),
        [flags.issues, orchestration, agents]
    );

    const computedNodes = useMemo(
        () => stepsToNodes(orchestration.steps, orchestration.entry_step_id, agents, selectedStepId, runStepStatuses, issuesByStep),
        [orchestration.steps, orchestration.entry_step_id, agents, selectedStepId, runStepStatuses, issuesByStep]
    );
    const computedEdges = useMemo(
        () => stepsToEdges(orchestration.steps, flags.edits),
        [orchestration.steps, flags.edits]
    );

    const [nodes, setNodes, onNodesChange] = useNodesState(computedNodes);
    const [edges, setEdges, onEdgesChange] = useEdgesState(computedEdges);

    // The orchestration is the source of truth; React Flow's copy exists for
    // drag interactions. Re-derive whenever the source changes.
    useEffect(() => { setNodes(computedNodes); }, [computedNodes, setNodes]);
    useEffect(() => { setEdges(computedEdges); }, [computedEdges, setEdges]);

    // Refit when switching to a different orchestration — not on every edit.
    const fittedForRef = useRef<string | null>(null);
    useEffect(() => {
        if (fittedForRef.current === orchestration.id) return;
        fittedForRef.current = orchestration.id;
        window.requestAnimationFrame(() => fitView({ padding: fitPadding }));
    }, [orchestration.id, fitView, fitPadding]);

    const onConnect = useCallback(
        (connection: Connection) => {
            const sourceStep = orchestration.steps.find((s) => s.id === connection.source);
            if (!sourceStep || !connection.target) return;

            const updatedSteps = orchestration.steps.map((s) => {
                // --- SOURCE step: update routing fields ---
                if (s.id === connection.source) {
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
            const { orchestration: next, step } = addStepToGraph(orchestration, type, position);
            onUpdateOrchestration(next);
            selectStep(step.id);
        },
        [orchestration, onUpdateOrchestration, selectStep]
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
        window.requestAnimationFrame(() => fitView({ padding: fitPadding, duration: 300 }));
    }, [getNodes, computedEdges, orchestration, onUpdateOrchestration, fitView, fitPadding]);

    const onNodeClick = useCallback(
        (_: any, node: Node) => {
            selectStep(node.id);
        },
        [selectStep]
    );

    const onPaneClick = useCallback(() => {
        selectStep(null);
    }, [selectStep]);

    const canvasButtonCls =
        'flex items-center gap-1.5 rounded-lg border border-border bg-surface px-2.5 py-2 text-xs text-text-muted shadow-md transition-colors hover:text-text';

    return (
        <div className="h-full w-full">
            <ReactFlow
                nodes={nodes}
                edges={edges}
                onNodesChange={onNodesChange}
                onEdgesChange={onEdgesChange}
                onConnect={flags.edits ? onConnect : undefined}
                onNodeDragStop={flags.edits ? onNodeDragStop : undefined}
                onNodeClick={onSelectStep ? onNodeClick : undefined}
                onPaneClick={onSelectStep ? onPaneClick : undefined}
                onDelete={flags.edits ? onDelete : undefined}
                onDragOver={flags.edits ? onDragOver : undefined}
                onDrop={flags.edits ? onDrop : undefined}
                nodesDraggable={flags.nodesDraggable}
                nodesConnectable={flags.nodesConnectable}
                elementsSelectable={flags.elementsSelectable}
                nodesFocusable={flags.nodesFocusable}
                edgesFocusable={flags.edgesFocusable}
                nodeTypes={nodeTypes}
                zoomOnScroll={flags.zoomOnScroll}
                preventScrolling={flags.preventScrolling}
                panOnDrag={flags.panOnDrag}
                // Not dragging is not enough for a finger to scroll the page: xyflow
                // 12.11's stylesheet gives the pane `touch-action: none`, so the
                // browser still will not scroll from it. When the drag is given
                // back to the page, the pane's touch is too. `!` because that
                // stylesheet is unlayered, and an unlayered rule beats any rule in
                // Tailwind's utilities layer whatever its specificity; `\_` because
                // an unescaped underscore in an arbitrary variant is a space.
                className={flags.panOnDrag ? undefined : String.raw`[&_.react-flow\_\_pane]:!touch-auto`}
                fitView
                fitViewOptions={{ padding: fitPadding }}
                minZoom={flags.minZoom}
                maxZoom={2}
                snapToGrid={flags.snapToGrid}
                snapGrid={[12, 12]}
                deleteKeyCode={flags.deleteKeyCode}
                proOptions={{ hideAttribution: true }}
                defaultEdgeOptions={{ type: 'smoothstep' }}
            >
                <Background variant={BackgroundVariant.Dots} gap={20} size={1} />
                {flags.minimap && (
                    <MiniMap
                        pannable
                        zoomable
                        nodeColor={(node: any) => {
                            const type = node.data?.step?.type;
                            return STEP_TYPE_META[type as keyof typeof STEP_TYPE_META]?.color || '#6b7280';
                        }}
                        maskColor="var(--minimap-mask)"
                    />
                )}
                {flags.palette && (
                    // The height bound lives on the Panel: it is absolutely
                    // positioned inside `.react-flow`, so percentage heights
                    // resolve against the canvas — on the palette div they
                    // resolved against an auto-height wrapper and the list
                    // could never scroll.
                    // 1rem = the Panel's own m-2 twice over, so the rail ends
                    // the same 8px from the bottom edge as it starts from the top.
                    <Panel position="top-left" className="!m-2 flex max-h-[calc(100%-1rem)]">
                        <StepPalette onAdd={addStep} />
                    </Panel>
                )}
                {/* One cluster for every canvas control — the default <Controls>
                    sat alone in the bottom-left corner, a second home for
                    buttons that belong with Tidy up. */}
                <Panel position="top-right" className="!m-2 flex gap-1.5">
                    {flags.tidyUp && (
                        <button type="button" onClick={tidyUp} title="Auto-arrange the graph left to right" className={canvasButtonCls}>
                            <Wand2 size={13} aria-hidden /> Tidy up
                        </button>
                    )}
                    <button
                        type="button"
                        onClick={() => zoomIn({ duration: 150 })}
                        title="Zoom in"
                        aria-label="Zoom in"
                        className={canvasButtonCls}
                    >
                        <Plus size={13} aria-hidden />
                    </button>
                    <button
                        type="button"
                        onClick={() => zoomOut({ duration: 150 })}
                        title="Zoom out"
                        aria-label="Zoom out"
                        className={canvasButtonCls}
                    >
                        <Minus size={13} aria-hidden />
                    </button>
                    <button
                        type="button"
                        onClick={() => fitView({ padding: fitPadding, duration: 300 })}
                        title="Fit the whole graph in view"
                        aria-label="Fit view"
                        className={canvasButtonCls}
                    >
                        <Maximize size={13} aria-hidden />
                    </button>
                </Panel>
            </ReactFlow>
        </div>
    );
}
