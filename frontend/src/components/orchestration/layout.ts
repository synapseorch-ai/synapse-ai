/**
 * "Tidy up" — one-shot dagre layout for the canvas.
 *
 * Deliberately not automatic: positions are authored data (`position_x/y`
 * persist with the step), and a layout that reflows on every edit fights the
 * author. This runs only when asked, writes the result back through the same
 * position fields a drag writes, and is therefore a single undo entry.
 *
 * Left-to-right because every edge in this graph flows source→target on the
 * horizontal handles; `dagre` over `elkjs` because the graphs are small
 * (tens of nodes) and dagre is ~30 KB against elk's ~1.5 MB wasm.
 */

import dagre from '@dagrejs/dagre';
import type { Node, Edge } from '@xyflow/react';

const FALLBACK_WIDTH = 200;
const FALLBACK_HEIGHT = 90;

export function layoutPositions(
    nodes: Node[],
    edges: Edge[],
): Record<string, { x: number; y: number }> {
    const g = new dagre.graphlib.Graph();
    g.setGraph({ rankdir: 'LR', nodesep: 48, ranksep: 96, marginx: 40, marginy: 40 });
    g.setDefaultEdgeLabel(() => ({}));

    for (const node of nodes) {
        g.setNode(node.id, {
            width: node.measured?.width ?? FALLBACK_WIDTH,
            height: node.measured?.height ?? FALLBACK_HEIGHT,
        });
    }
    for (const edge of edges) {
        if (g.hasNode(edge.source) && g.hasNode(edge.target)) {
            g.setEdge(edge.source, edge.target);
        }
    }

    dagre.layout(g);

    const positions: Record<string, { x: number; y: number }> = {};
    for (const node of nodes) {
        const placed = g.node(node.id);
        if (!placed) continue;
        // dagre reports centers; React Flow positions are top-left corners.
        positions[node.id] = {
            x: Math.round(placed.x - (node.measured?.width ?? FALLBACK_WIDTH) / 2),
            y: Math.round(placed.y - (node.measured?.height ?? FALLBACK_HEIGHT) / 2),
        };
    }
    return positions;
}
