'use client';
/* eslint-disable @typescript-eslint/no-explicit-any */
/**
 * The open canvas (Prototype 4, D204, M3a): the workflow editor as a surface
 * the graph fills edge to edge, with everything else floating over it.
 *
 * - a bar at the top: back, the name, the save state; what to check, Change
 *   with AI, Run now or Stop, help, ⋯
 * - a setup card at the right that holds a step's whole setup, or the
 *   workflow's settings; a bottom sheet on a phone
 * - a toolbar over the selected step; a dock (Add step, Tidy up, Undo, Redo);
 *   zoom and Fit at the bottom right, with a minimap that is off until asked for
 * - the run strip at the bottom left, opening the run's log above it
 *
 * Built once, in the synced folder: OSS's editor uses it now and cloud's in
 * M3b. The workbench owns the layout and the canvas's behaviour; each product
 * supplies what is its own — the step panel with its product props, the
 * settings it has, the run's strip and log, the ⋯ entries. The classic
 * `WorkflowCanvas` is unchanged beside it and shares its graph
 * (`useWorkflowGraph`).
 */
import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react';
import {
    Background,
    BackgroundVariant,
    MiniMap,
    NodeToolbar,
    Position,
    ReactFlow,
    ReactFlowProvider,
    useReactFlow,
    type Node,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import { Copy, Flag, Trash2 } from 'lucide-react';
import { CommandPalette, IconButton, useCanvasMode, usePersisted, useRegisterCommands } from '@/components/ui';
import { cn } from '@/lib/cn';
import { KNOWLEDGE_STEP_TYPES, type Orchestration, type StepConfig, type StepType } from '@/types/orchestration';
import { canvasFlags } from './canvasFlags';
import { ROLE_FILL, roleOf } from './roles';
import { nodeTypes, useWorkflowGraph, type RunStepStatus } from './useWorkflowGraph';
import { validateOrchestration } from './validate';
import { Dock, ZoomCluster } from './workbench/Controls';
import {
    CARD_DEFAULT, CARD_MAX, CARD_MIN, PHONE_QUERY, fitPadding, freeRect, panToFit, tierOf, type Rect,
} from './workbench/geometry';
import { useElementBox, useMediaQuery, useReducedMotion } from './workbench/hooks';
import { SetupCard } from './workbench/SetupCard';
import { stepCommandGroups } from './workbench/stepCommands';
import { FLOAT, TopBar } from './workbench/TopBar';
import type { WorkbenchIssue, WorkbenchMenuItem, WorkbenchRunPhase } from './workbench/types';

export type { WorkbenchIssue, WorkbenchMenuItem, WorkbenchRunPhase };

export interface CanvasWorkbenchProps {
    orchestration: Orchestration;
    /** Every edit: the canvas's, the name's, the toolbar's. The host records it for undo. */
    onChange: (next: Orchestration) => void;
    agents: any[];
    /** Retrieve and Ingest are offered only with Knowledge on. */
    knowledgeEnabled?: boolean;
    selectedStepId: string | null;
    onSelectStep: (stepId: string | null) => void;
    /** The step's setup, as the product renders it: `StepConfigPanel frame="card"` with its own props. */
    renderStep: (step: StepConfig) => React.ReactNode;
    /** The workflow's settings, as the product composes them (`WorkflowSettingsPanel` and its sections). */
    renderSettings: (close: () => void) => React.ReactNode;
    /** The step toolbar's actions, the same ones the panel and ⌘D use. */
    stepActions: {
        duplicate: (stepId: string) => void;
        setEntry: (stepId: string) => void;
        remove: (stepId: string) => void;
    };
    history: { undo: () => void; redo: () => void; canUndo: boolean; canRedo: boolean };
    save: { dirty: boolean; saving: boolean; onSave: () => void };
    onBack: () => void;
    /** Beside the name: switch to another workflow. */
    switcher?: React.ReactNode;
    /** The ⋯ menu's entries after Workflow settings. */
    menu: WorkbenchMenuItem[];
    /** Opens the product's builder on this workflow. */
    onChangeWithAI?: () => void;
    /** How much of the window's right edge a shell panel covers (the builder's), so the chrome keeps clear of it. */
    insetRight?: number;
    run: {
        phase: WorkbenchRunPhase;
        stepStatuses?: Record<string, RunStepStatus>;
        stepErrors?: Record<string, string>;
        onRun: () => void;
        onStop: () => void;
        /** The run's input, behind Run now's chevron. */
        input?: { value: string; onChange: (value: string) => void };
    };
    /** The run strip's content, at the bottom left. */
    runStrip?: React.ReactNode;
    /** The run's log and recent runs, opened from the strip above it. */
    runDrawer?: { open: boolean; content: React.ReactNode };
}

export function CanvasWorkbench(props: CanvasWorkbenchProps) {
    return (
        <ReactFlowProvider>
            <Workbench {...props} />
        </ReactFlowProvider>
    );
}

const COARSE = '(pointer: coarse)';
function subscribeCoarse(onChange: () => void) {
    const media = window.matchMedia(COARSE);
    media.addEventListener('change', onChange);
    return () => media.removeEventListener('change', onChange);
}

function Workbench({
    orchestration, onChange, agents, knowledgeEnabled = false, selectedStepId, onSelectStep,
    renderStep, renderSettings, stepActions, history, save, onBack, switcher, menu, onChangeWithAI,
    insetRight = 0, run, runStrip, runDrawer,
}: CanvasWorkbenchProps) {
    // The canvas is the page: the shell hides the page header and folds the menu (D204).
    useCanvasMode(true);

    const { fitView, getInternalNode, getViewport, setViewport, zoomIn, zoomOut, zoomTo } = useReactFlow();
    const rootRef = useRef<HTMLDivElement>(null);
    const flowRef = useRef<HTMLDivElement>(null);
    const sheetRef = useRef<HTMLDivElement>(null);
    const dockRef = useRef<HTMLDivElement>(null);
    const box = useElementBox(rootRef);
    const phone = useMediaQuery(PHONE_QUERY);
    const reduced = useReducedMotion();
    const coarsePointer = useSyncExternalStore(subscribeCoarse, () => window.matchMedia(COARSE).matches, () => false);
    const flags = canvasFlags({ readOnly: false, showPalette: false, coarsePointer });

    // ── The card: a step's setup, or the workflow's settings ──────────────
    const selectedStep = orchestration.steps.find((s) => s.id === selectedStepId) ?? null;
    const [settingsOpen, setSettingsOpen] = useState(false);
    // Selecting a step shows its setup; the settings give way (adjusted while
    // rendering, not in an effect).
    const [seenSelection, setSeenSelection] = useState(selectedStepId);
    if (seenSelection !== selectedStepId) {
        setSeenSelection(selectedStepId);
        if (selectedStepId && settingsOpen) setSettingsOpen(false);
    }
    const view: 'step' | 'settings' | null = selectedStep ? 'step' : settingsOpen ? 'settings' : null;
    const cardOpen = view !== null;
    const sheetBox = useElementBox(sheetRef, cardOpen && phone);
    const dockBox = useElementBox(dockRef);

    const [storedWidth, setStoredWidth] = usePersisted<string>(
        'orch-card-width',
        (raw) => {
            const n = parseInt(raw ?? '');
            return Number.isFinite(n) && n >= CARD_MIN && n <= CARD_MAX ? String(n) : String(CARD_DEFAULT);
        },
        String(CARD_DEFAULT),
    );
    const [dragWidth, setDragWidth] = useState<number | null>(null);
    const cardWidth = dragWidth ?? parseInt(storedWidth);

    const [minimapFlag, setMinimapFlag] = usePersisted<'0' | '1'>('orch-minimap', (raw) => (raw === '1' ? '1' : '0'), '0');
    const minimap = minimapFlag === '1';

    // ── Where the graph can be seen ───────────────────────────────────────
    const localInset = Math.max(0, insetRight - box.right);
    const cardSpace = cardOpen && !phone ? cardWidth : 0;
    const sheetSpace = cardOpen && phone ? sheetBox.height : 0;
    const freeWidth = box.width - localInset - (cardSpace ? cardSpace + 24 : 0);
    const tier = tierOf(freeWidth);
    const live = useRef({ card: cardSpace, sheet: sheetSpace, inset: localInset });
    useEffect(() => {
        live.current = { card: cardSpace, sheet: sheetSpace, inset: localInset };
    });
    /** The free rectangle now, measured rather than from the last render (a fit can run before the first measure). */
    const freeNow = useCallback((): { rect: Rect; width: number; height: number } | null => {
        const el = rootRef.current;
        if (!el) return null;
        const r = el.getBoundingClientRect();
        const rect = freeRect({ width: r.width, height: r.height, ...live.current, insetRight: live.current.inset });
        return { rect, width: r.width, height: r.height };
    }, []);

    const fit = useCallback((duration?: number) => {
        const now = freeNow();
        if (!now) return;
        fitView({ padding: fitPadding(now.rect, now.width, now.height), duration: reduced ? 0 : duration });
    }, [fitView, freeNow, reduced]);

    const graph = useWorkflowGraph({
        orchestration,
        agents,
        selectedStepId,
        onSelectStep,
        onUpdateOrchestration: onChange,
        runStepStatuses: run.stepStatuses,
        runStepErrors: run.stepErrors,
        flags,
        fit,
        wrapperRef: flowRef,
    });

    // The selected step slides into view if the card or the bar would cover it.
    useEffect(() => {
        if (!selectedStepId) return;
        const raf = window.requestAnimationFrame(() => {
            const node = getInternalNode(selectedStepId);
            const now = freeNow();
            if (!node || !now) return;
            const vp = getViewport();
            const x = node.internals.positionAbsolute.x * vp.zoom + vp.x;
            const y = node.internals.positionAbsolute.y * vp.zoom + vp.y;
            const w = (node.measured?.width ?? 200) * vp.zoom;
            const h = (node.measured?.height ?? 90) * vp.zoom;
            // Room above the step for its toolbar.
            const roomy = { ...now.rect, top: now.rect.top + 44 };
            const { dx, dy } = panToFit({ left: x, top: y, right: x + w, bottom: y + h }, roomy);
            if (dx || dy) setViewport({ x: vp.x + dx, y: vp.y + dy, zoom: vp.zoom }, { duration: reduced ? 0 : 200 });
        });
        return () => window.cancelAnimationFrame(raf);
    }, [selectedStepId, cardOpen, cardWidth, phone, sheetBox.height, getInternalNode, getViewport, setViewport, freeNow, reduced]);

    const closeCard = useCallback(() => {
        const was = selectedStepId;
        setSettingsOpen(false);
        if (was) {
            onSelectStep(null);
            // Back to the step it was about, as a click on the pane would leave it.
            window.requestAnimationFrame(() => {
                const el = rootRef.current?.querySelector<HTMLElement>(`.react-flow__node[data-id="${CSS.escape(was)}"]`);
                el?.focus();
            });
        }
    }, [selectedStepId, onSelectStep]);

    const openSettings = useCallback(() => {
        onSelectStep(null);
        setSettingsOpen(true);
    }, [onSelectStep]);

    // Esc closes the card: after an open menu, list or dialog has taken its own
    // Esc (they mark it handled), and after a field gives up the focus.
    useEffect(() => {
        if (!cardOpen) return;
        const onKey = (event: KeyboardEvent) => {
            if (event.key !== 'Escape' || event.defaultPrevented) return;
            const target = event.target as HTMLElement | null;
            const root = rootRef.current;
            if (target && target !== document.body && root && !root.contains(target)) return;
            if (target?.closest?.('input, textarea, select, [contenteditable="true"], .cm-editor')) {
                (document.activeElement as HTMLElement | null)?.blur();
                return;
            }
            closeCard();
        };
        document.addEventListener('keydown', onKey);
        return () => document.removeEventListener('keydown', onKey);
    }, [cardOpen, closeCard]);

    // ── Things to check ───────────────────────────────────────────────────
    const validation = useMemo(() => validateOrchestration(orchestration, agents), [orchestration, agents]);
    const issues: WorkbenchIssue[] = useMemo(() => [
        ...validation.global.map((i) => ({ ...i, stepId: null })),
        ...Object.entries(validation.byStep).flatMap(([stepId, list]) => {
            const step = orchestration.steps.find((s) => s.id === stepId);
            return list.map((i) => ({ ...i, stepId, stepName: step?.name || stepId }));
        }),
    ], [validation, orchestration.steps]);
    const onIssue = useCallback((issue: WorkbenchIssue) => {
        if (issue.stepId) onSelectStep(issue.stepId);
        else openSettings();
    }, [onSelectStep, openSettings]);

    // ── Adding steps: the dock's list, and the same items in the app's ⌘K ─
    const hiddenTypes = knowledgeEnabled ? undefined : KNOWLEDGE_STEP_TYPES;
    const [picker, setPicker] = useState(false);
    const addStep = graph.addStep;
    const pickerGroups = useMemo(
        () => stepCommandGroups({ onAdd: (t: StepType) => { addStep(t); }, hiddenTypes, grouped: true }),
        [addStep, hiddenTypes],
    );
    const appGroup = useMemo(
        () => stepCommandGroups({ onAdd: (t: StepType) => { addStep(t); }, hiddenTypes, grouped: false })[0],
        [addStep, hiddenTypes],
    );
    useRegisterCommands('canvas-steps', appGroup);

    // ── The step toolbar hides while a step is dragged ────────────────────
    const [dragging, setDragging] = useState(false);
    const isEntry = !!selectedStep && orchestration.entry_step_id === selectedStep.id;

    const changeWithAI = onChangeWithAI
        ? () => {
            closeCard();
            onChangeWithAI();
        }
        : undefined;

    // ── Layout of the bottom row ──────────────────────────────────────────
    const dockCentre = phone ? box.width / 2 : (box.width - localInset - (cardSpace ? cardSpace + 24 : 0)) / 2;
    const zoomRight = 12 + localInset + (cardSpace ? cardSpace + 12 : 0);
    // The strip runs up to the dock, and wraps to a second row rather than under it.
    const stripMax = phone ? undefined : Math.max(240, dockCentre - (dockBox.width || 320) / 2 - 24);
    const drawerWidth = Math.max(280, Math.min(640, freeWidth - 24));
    const [drawerHeight, setDrawerHeight] = useState(320);
    // Clear of the bar above and of a strip that may wrap to two rows below.
    const drawerMax = Math.max(120, Math.min(700, box.height - (phone ? 260 : 200)));
    const resizeDrawer = (event: React.PointerEvent) => {
        event.preventDefault();
        const startY = event.clientY;
        const startH = drawerHeight;
        const onMove = (e: PointerEvent) => setDrawerHeight(Math.max(120, Math.min(drawerMax, startH + (startY - e.clientY))));
        window.addEventListener('pointermove', onMove);
        window.addEventListener('pointerup', () => window.removeEventListener('pointermove', onMove), { once: true });
    };
    const resizeDrawerKey = (event: React.KeyboardEvent) => {
        const step = event.key === 'ArrowUp' ? 16 : event.key === 'ArrowDown' ? -16 : 0;
        if (!step) return;
        event.preventDefault();
        setDrawerHeight((h) => Math.max(120, Math.min(drawerMax, h + step)));
    };

    return (
        <div ref={rootRef} className="relative h-full w-full overflow-hidden bg-bg" data-canvas-workbench="">
            {/* The page's title, for the outline and for anything that looks for one: the bar shows it as a field. */}
            <h1 className="sr-only">{orchestration.name || 'Untitled workflow'}</h1>

            <div
                ref={flowRef}
                className="absolute inset-0"
                onKeyDown={(event) => {
                    graph.onCanvasKeyDown(event);
                    // xyflow blurs a step on Escape: put the focus back on it, where the
                    // keyboard was, now that its card has closed.
                    const node = (event.target as HTMLElement).closest?.('.react-flow__node');
                    // It blurs in a frame of its own, and a focus in that same frame does
                    // not hold, so this waits until just after it.
                    if (event.key === 'Escape' && node === event.target) {
                        window.requestAnimationFrame(() => window.setTimeout(() => (node as HTMLElement).focus(), 30));
                    }
                }}
            >
                <ReactFlow
                    nodes={graph.nodes}
                    edges={graph.edges}
                    onNodesChange={graph.onNodesChange}
                    onEdgesChange={graph.onEdgesChange}
                    onConnect={graph.onConnect}
                    onNodeDragStart={() => setDragging(true)}
                    onNodeDragStop={(event: any, node: Node, nodes: Node[]) => {
                        setDragging(false);
                        graph.onNodeDragStop(event, node, nodes);
                    }}
                    onNodeClick={graph.onNodeClick}
                    onPaneClick={() => {
                        setSettingsOpen(false);
                        graph.onPaneClick();
                    }}
                    onDelete={graph.onDelete}
                    onDragOver={graph.onDragOver}
                    onDrop={graph.onDrop}
                    nodesDraggable={flags.nodesDraggable}
                    nodesConnectable={flags.nodesConnectable}
                    elementsSelectable={flags.elementsSelectable}
                    nodesFocusable={flags.nodesFocusable}
                    edgesFocusable={flags.edgesFocusable}
                    nodeTypes={nodeTypes}
                    zoomOnScroll={flags.zoomOnScroll}
                    preventScrolling={flags.preventScrolling}
                    panOnDrag={flags.panOnDrag}
                    minZoom={flags.minZoom}
                    maxZoom={2}
                    snapToGrid={flags.snapToGrid}
                    snapGrid={[12, 12]}
                    deleteKeyCode={graph.deleteKeyCode}
                    proOptions={{ hideAttribution: true }}
                    defaultEdgeOptions={{ type: 'smoothstep' }}
                >
                    <Background variant={BackgroundVariant.Dots} gap={20} size={1} />
                    {minimap && !phone && tier !== 'tight' && (
                        <MiniMap
                            pannable
                            zoomable
                            ariaLabel="Minimap"
                            // By who does the work (D211), as the steps themselves are.
                            nodeColor={(node: any) => ROLE_FILL[roleOf(node.data?.step?.type)]}
                            maskColor="var(--minimap-mask)"
                            className="!m-0"
                            style={{ right: zoomRight, bottom: 62, width: 204, height: 124 }}
                        />
                    )}
                    <NodeToolbar
                        nodeId={selectedStepId ?? undefined}
                        isVisible={!!selectedStepId && !dragging}
                        position={Position.Top}
                        offset={10}
                        className="nokey nodrag nopan"
                    >
                        {selectedStep && (
                            <div role="toolbar" aria-label="Step actions" className={cn(FLOAT, 'flex items-center gap-0.5 p-0.5')}>
                                <IconButton label="Duplicate (Ctrl+D)" icon={Copy} size="sm" onClick={() => stepActions.duplicate(selectedStep.id)} />
                                <IconButton
                                    label={isEntry ? 'Already the start' : 'Set as start'}
                                    icon={Flag}
                                    size="sm"
                                    onClick={() => stepActions.setEntry(selectedStep.id)}
                                    disabled={isEntry}
                                />
                                <IconButton label="Delete (Del)" icon={Trash2} size="sm" tone="danger" onClick={() => stepActions.remove(selectedStep.id)} />
                            </div>
                        )}
                    </NodeToolbar>
                </ReactFlow>
            </div>

            <TopBar
                name={orchestration.name}
                onRename={(name) => onChange({ ...orchestration, name })}
                onBack={onBack}
                switcher={switcher}
                dirty={save.dirty}
                saving={save.saving}
                onSave={save.onSave}
                issues={issues}
                onIssue={onIssue}
                onChangeWithAI={changeWithAI}
                phase={run.phase}
                onRun={run.onRun}
                onStop={run.onStop}
                blocked={validation.errorCount}
                runInput={run.input}
                menu={menu}
                onOpenSettings={openSettings}
                settingsOpen={view === 'settings'}
                phone={phone}
                compact={tier !== 'full'}
                insetRight={localInset}
            />

            {cardOpen && (
                <SetupCard
                    width={cardWidth}
                    onWidth={setDragWidth}
                    onWidthDone={(w) => {
                        setStoredWidth(String(w));
                        setDragWidth(null);
                    }}
                    phone={phone}
                    insetRight={localInset}
                    onClose={closeCard}
                    sheetRef={sheetRef}
                >
                    {view === 'step' && selectedStep ? renderStep(selectedStep) : renderSettings(() => setSettingsOpen(false))}
                </SetupCard>
            )}

            <Dock
                boxRef={dockRef}
                left={dockCentre}
                phone={phone}
                tier={tier}
                onAddStep={() => setPicker(true)}
                onTidy={graph.tidyUp}
                tidy={flags.tidyUp}
                undo={history.undo}
                redo={history.redo}
                canUndo={history.canUndo}
                canRedo={history.canRedo}
            />
            {!(phone && cardOpen) && (
                <ZoomCluster
                    right={phone ? 8 : zoomRight}
                    tier={phone ? 'tight' : tier}
                    minimap={minimap}
                    onMinimap={(on) => setMinimapFlag(on ? '1' : '0')}
                    zoomIn={() => zoomIn({ duration: reduced ? 0 : 150 })}
                    zoomOut={() => zoomOut({ duration: reduced ? 0 : 150 })}
                    zoomTo={(z) => zoomTo(z, { duration: reduced ? 0 : 150 })}
                    fit={() => fit(300)}
                />
            )}

            {/* The run strip at the bottom left, and its drawer above it in one column
                that grows upward: the drawer can never sit over the strip, however the
                strip wraps (a measured offset was a frame late when a question arrived). */}
            {(runStrip || runDrawer?.open) && (
                <div
                    className={cn('nokey pointer-events-none absolute z-20 flex flex-col items-start gap-2', phone ? 'inset-x-2 bottom-[56px]' : 'bottom-3 left-3')}
                    style={phone ? undefined : { maxWidth: Math.max(stripMax ?? 0, drawerWidth) }}
                >
                    {runDrawer?.open && (
                        <div
                            data-canvas-chrome="drawer"
                            className={cn(FLOAT, 'pointer-events-auto flex flex-col overflow-hidden rounded-[12px]', phone && 'w-full')}
                            style={{ width: phone ? undefined : drawerWidth, height: Math.min(drawerHeight, drawerMax) }}
                        >
                            <div
                                role="separator"
                                aria-orientation="horizontal"
                                aria-label="Resize the run log"
                                aria-valuemin={120}
                                aria-valuemax={drawerMax}
                                aria-valuenow={drawerHeight}
                                tabIndex={0}
                                onPointerDown={resizeDrawer}
                                onKeyDown={resizeDrawerKey}
                                className="group flex h-2 shrink-0 cursor-ns-resize items-center justify-center outline-none"
                            >
                                <span className="h-[3px] w-9 rounded-full bg-border-strong opacity-0 transition-opacity group-hover:opacity-100 group-focus-visible:opacity-100" aria-hidden />
                            </div>
                            <div className="flex min-h-0 flex-1 flex-col">{runDrawer.content}</div>
                        </div>
                    )}
                    {runStrip && (
                        <div
                            data-canvas-chrome="strip"
                            className={cn('pointer-events-auto', phone && 'w-full')}
                            style={stripMax ? { maxWidth: stripMax } : undefined}
                        >
                            {runStrip}
                        </div>
                    )}
                </div>
            )}

            <CommandPalette
                open={picker}
                onOpenChange={setPicker}
                groups={pickerGroups}
                placeholder="Add a step… (drag one onto the canvas, or pick it)"
                label="Add a step"
            />
        </div>
    );
}
