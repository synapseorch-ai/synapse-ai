'use client';
/* eslint-disable @typescript-eslint/no-explicit-any */
import { useMemo, useRef, useSyncExternalStore } from 'react';
import {
    ReactFlow,
    Background,
    MiniMap,
    Panel,
    useReactFlow,
    BackgroundVariant,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import { Maximize, Minus, Plus, Wand2 } from 'lucide-react';
import { canvasFlags } from './canvasFlags';
import { PALETTE_CLEARANCE_PX, StepPalette, usePaletteCollapsed } from './StepPalette';
import { ROLE_FILL, roleOf } from './roles';
import { nodeTypes, useWorkflowGraph } from './useWorkflowGraph';
import type { Orchestration } from '@/types/orchestration';
import { KNOWLEDGE_STEP_TYPES } from '@/types/orchestration';

interface WorkflowCanvasProps {
    orchestration: Orchestration;
    agents: any[];
    selectedStepId?: string | null;
    /** Omit to make steps inert to clicks — a preview with nothing to open. */
    onSelectStep?: (stepId: string | null) => void;
    /** Required unless `readOnly`: every edit on the canvas arrives here. */
    onUpdateOrchestration?: (orch: Orchestration) => void;
    runStepStatuses?: Record<string, 'pending' | 'running' | 'paused' | 'completed' | 'failed'>;
    /** Why each failed step failed, in its own words (F195); shown on its card. */
    runStepErrors?: Record<string, string>;
    /** The palette rail is part of the canvas; a shell that renders its own can turn it off. */
    showPalette?: boolean;
    /** Look, don't touch: no drag, connect, delete, drop, palette, Tidy up or issue badges. See `canvasFlags`. */
    readOnly?: boolean;
    /**
     * Whether Knowledge is on: its Retrieve and Ingest steps are in the palette
     * only then. Unset reads as off, so a shell that has not wired it offers
     * neither step rather than steps that would fail their runs.
     */
    knowledgeEnabled?: boolean;
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
/** Fit-view's air on each side, as a fraction of the canvas. */
const FIT_PADDING = 0.15;

export function WorkflowCanvas({
    orchestration,
    agents,
    selectedStepId = null,
    onSelectStep,
    onUpdateOrchestration = noop,
    runStepStatuses,
    runStepErrors,
    showPalette = true,
    readOnly = false,
    knowledgeEnabled = false,
}: WorkflowCanvasProps) {
    const { fitView, zoomIn, zoomOut } = useReactFlow();
    const coarsePointer = useSyncExternalStore(subscribeCoarse, readCoarse, serverCoarse);
    const flags = canvasFlags({ readOnly, showPalette, coarsePointer });
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

    const wrapperRef = useRef<HTMLDivElement>(null);
    const graph = useWorkflowGraph({
        orchestration,
        agents,
        selectedStepId,
        onSelectStep,
        onUpdateOrchestration,
        runStepStatuses,
        runStepErrors,
        flags,
        fit: (duration) => fitView({ padding: fitPadding, duration }),
        wrapperRef,
    });

    const canvasButtonCls =
        'flex items-center gap-1.5 rounded-lg border border-border bg-surface px-2.5 py-2 text-xs text-text-muted shadow-md transition-colors hover:text-text';

    return (
        <div ref={wrapperRef} className="h-full w-full" onKeyDown={onSelectStep ? graph.onCanvasKeyDown : undefined}>
            <ReactFlow
                nodes={graph.nodes}
                edges={graph.edges}
                onNodesChange={graph.onNodesChange}
                onEdgesChange={graph.onEdgesChange}
                onConnect={flags.edits ? graph.onConnect : undefined}
                onNodeDragStop={flags.edits ? graph.onNodeDragStop : undefined}
                onNodeClick={onSelectStep ? graph.onNodeClick : undefined}
                onPaneClick={onSelectStep ? graph.onPaneClick : undefined}
                onDelete={flags.edits ? graph.onDelete : undefined}
                onDragOver={flags.edits ? graph.onDragOver : undefined}
                onDrop={flags.edits ? graph.onDrop : undefined}
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
                deleteKeyCode={graph.deleteKeyCode}
                proOptions={{ hideAttribution: true }}
                defaultEdgeOptions={{ type: 'smoothstep' }}
            >
                <Background variant={BackgroundVariant.Dots} gap={20} size={1} />
                {flags.minimap && (
                    <MiniMap
                        pannable
                        zoomable
                        // By who does the work (D211), as the steps themselves are.
                        nodeColor={(node: any) => ROLE_FILL[roleOf(node.data?.step?.type)]}
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
                        <StepPalette onAdd={graph.addStep} hiddenTypes={knowledgeEnabled ? undefined : KNOWLEDGE_STEP_TYPES} />
                    </Panel>
                )}
                {/* One cluster for every canvas control — the default <Controls>
                    sat alone in the bottom-left corner, a second home for
                    buttons that belong with Tidy up. */}
                <Panel position="top-right" className="!m-2 flex gap-1.5">
                    {flags.tidyUp && (
                        <button type="button" onClick={graph.tidyUp} title="Auto-arrange the graph left to right" className={canvasButtonCls}>
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
