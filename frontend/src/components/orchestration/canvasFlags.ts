/**
 * What the canvas lets a person do, as plain data.
 *
 * One function so the answer is testable without a DOM (the cloud product
 * tests it from `web/src/lib/canvasFlags.test.ts`; this directory has no test
 * runner) and so "read-only" means one thing everywhere it is used: a template
 * preview, the builder showing its own shape, a viewer's editor.
 *
 * Read-only turns off every path that writes to the orchestration — drag,
 * connect, delete, drop from the palette, Tidy up — and the issue badges,
 * which describe a draft someone is meant to fix. Viewing stays: pan, zoom,
 * fit, and clicking a step when the shell asks to hear about it.
 *
 * A preview is small and has to show the whole graph at once: the minimap
 * would cover a third of it, and the editor's zoom floor stops a wide
 * orchestration (the builder is 4,000px across) from fitting at all.
 *
 * Edge selection needs its own flag: `edge()` sets `selectable` explicitly, and
 * in xyflow an explicit `true` on an edge outranks `elementsSelectable={false}`.
 *
 * A preview sits in a page or a modal that scrolls, and must not take the
 * scroll away from it. xyflow's defaults do exactly that: the wheel over the
 * canvas zooms it and the page stays put, and on a phone a one-finger drag pans
 * it and the page stays put (both measured on the template page). Read-only
 * lets a plain wheel through — Ctrl+wheel, which is how a trackpad pinches,
 * still zooms. Touch cannot be told apart from a mouse drag in xyflow, so the
 * shell says whether the primary pointer is coarse; there a finger scrolls the
 * page and the zoom buttons do the zooming. The editor keeps all three.
 */
export interface CanvasFlags {
    nodesDraggable: boolean;
    nodesConnectable: boolean;
    elementsSelectable: boolean;
    nodesFocusable: boolean;
    edgesFocusable: boolean;
    /** Null leaves no key that deletes. */
    deleteKeyCode: string[] | null;
    snapToGrid: boolean;
    /** Connect, drag-stop, delete and drop handlers wired; edges deletable and selectable. */
    edits: boolean;
    palette: boolean;
    tidyUp: boolean;
    issues: boolean;
    minimap: boolean;
    minZoom: number;
    zoomOnScroll: boolean;
    /** False lets a plain wheel scroll the page around the canvas. */
    preventScrolling: boolean;
    panOnDrag: boolean;
}

export function canvasFlags({
    readOnly = false,
    showPalette = true,
    coarsePointer = false,
}: {
    readOnly?: boolean;
    showPalette?: boolean;
    /** `(pointer: coarse)` — a phone or tablet, where a drag is how the page scrolls. */
    coarsePointer?: boolean;
} = {}): CanvasFlags {
    const editable = !readOnly;
    return {
        nodesDraggable: editable,
        nodesConnectable: editable,
        elementsSelectable: editable,
        nodesFocusable: editable,
        edgesFocusable: editable,
        deleteKeyCode: editable ? ['Delete', 'Backspace'] : null,
        snapToGrid: editable,
        edits: editable,
        palette: editable && showPalette,
        tidyUp: editable,
        issues: editable,
        minimap: editable,
        minZoom: editable ? 0.2 : 0.05,
        zoomOnScroll: editable,
        preventScrolling: editable,
        panOnDrag: editable || !coarsePointer,
    };
}
