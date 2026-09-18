'use client';

/**
 * The step palette — a left rail inside the canvas.
 *
 * Lives in this shared directory (not the shell) on purpose: rendered by
 * `WorkflowCanvas` as a React Flow `<Panel>`, so both products get it from the
 * sync with zero shell wiring. The old top strip was fourteen flat buttons
 * that overflowed narrow viewports and could only append at a computed grid
 * slot; this groups by category, filters, and is a drag source — drop a type
 * exactly where it belongs, or click to add at the frontier.
 */

import { useMemo, useState } from 'react';
import {
    Bot, Scale, GitBranch, GitMerge, RefreshCw, User, Code, Square, Zap, Wrench,
    Braces, GitFork, ArrowLeftRight, FileText, PanelLeftClose, PanelLeftOpen,
} from 'lucide-react';
import { SearchInput, usePersisted } from '@/components/ui';
import { STEP_TYPE_META, type StepCategory, type StepType } from '@/types/orchestration';

const ICONS: Record<string, React.FC<{ size?: number; className?: string }>> = {
    Bot, Scale, GitBranch, GitMerge, RefreshCw, User, Code, Square, Zap, Wrench,
    Braces, GitFork, ArrowLeftRight, FileText,
};

const CATEGORY_ORDER: StepCategory[] = ['Core', 'Logic', 'Data', 'I/O'];

/** The dataTransfer key the canvas' onDrop reads. */
export const STEP_DRAG_TYPE = 'application/x-synapse-step-type';

/**
 * How far the open palette reaches into the canvas: `w-56` plus its Panel's
 * `m-2`, plus the air fit-view leaves on every other side. The canvas fits the
 * graph to the right of this, or step 1 opens under the palette (F31).
 */
export const PALETTE_CLEARANCE_PX = 224 + 8 + 24;

/** Whether the palette is folded to its "Steps" button — shared with the canvas's fit-view. */
export function usePaletteCollapsed() {
    return usePersisted<'0' | '1'>('orch-palette-collapsed', (raw) => (raw === '1' ? '1' : '0'), '0');
}

export function StepPalette({ onAdd }: { onAdd: (type: StepType) => void }) {
    const [query, setQuery] = useState('');
    const [collapsed, setCollapsed] = usePaletteCollapsed();

    const groups = useMemo(() => {
        const q = query.trim().toLowerCase();
        const byCategory = new Map<StepCategory, StepType[]>();
        for (const [type, meta] of Object.entries(STEP_TYPE_META) as [StepType, (typeof STEP_TYPE_META)[StepType]][]) {
            if (q && !meta.label.toLowerCase().includes(q) && !meta.blurb.toLowerCase().includes(q)) continue;
            const bucket = byCategory.get(meta.category);
            if (bucket) bucket.push(type);
            else byCategory.set(meta.category, [type]);
        }
        return CATEGORY_ORDER.filter((c) => byCategory.has(c)).map((c) => [c, byCategory.get(c)!] as const);
    }, [query]);

    if (collapsed === '1') {
        return (
            <button
                type="button"
                onClick={() => setCollapsed('0')}
                title="Show step palette"
                aria-label="Show step palette"
                className="flex items-center gap-1.5 rounded-lg border border-border bg-surface px-2.5 py-2 text-xs text-text-muted shadow-md transition-colors hover:text-text"
            >
                <PanelLeftOpen size={14} aria-hidden /> Steps
            </button>
        );
    }

    return (
        <div className="flex max-h-full min-h-0 w-56 flex-col overflow-hidden rounded-lg border border-border bg-surface shadow-md">
            <div className="flex items-center gap-2 border-b border-border px-3 py-2">
                <span className="flex-1 text-xs font-semibold text-text">Steps</span>
                <button
                    type="button"
                    onClick={() => setCollapsed('1')}
                    title="Hide step palette"
                    aria-label="Hide step palette"
                    className="rounded-md p-0.5 text-text-faint transition-colors hover:text-text"
                >
                    <PanelLeftClose size={14} aria-hidden />
                </button>
            </div>
            <div className="border-b border-border p-2">
                <SearchInput
                    value={query}
                    onChange={setQuery}
                    placeholder="Filter steps…"
                    className="max-w-none [&_input]:py-1 [&_input]:text-xs"
                />
            </div>
            <div className="min-h-0 flex-1 space-y-3 overflow-y-auto p-2">
                {groups.length === 0 && (
                    <div className="px-1 py-3 text-center text-xs text-text-faint">No matching steps.</div>
                )}
                {groups.map(([category, types]) => (
                    <div key={category}>
                        <div className="px-1 pb-1 text-2xs font-medium uppercase tracking-wider text-text-faint">
                            {category}
                        </div>
                        <div className="space-y-0.5">
                            {types.map((type) => {
                                const meta = STEP_TYPE_META[type];
                                const Icon = ICONS[meta.icon] || Bot;
                                return (
                                    <button
                                        key={type}
                                        type="button"
                                        draggable
                                        onDragStart={(e) => {
                                            e.dataTransfer.setData(STEP_DRAG_TYPE, type);
                                            e.dataTransfer.effectAllowed = 'move';
                                        }}
                                        onClick={() => onAdd(type)}
                                        title={`${meta.blurb} — drag onto the canvas, or click to add`}
                                        className="flex w-full cursor-grab items-center gap-2 rounded-md px-2 py-1.5 text-left text-xs text-text-muted transition-colors hover:bg-surface-2 hover:text-text active:cursor-grabbing"
                                    >
                                        <span
                                            className="flex size-5 shrink-0 items-center justify-center rounded-md"
                                            style={{ backgroundColor: meta.color + '26', color: meta.color }}
                                            aria-hidden
                                        >
                                            <Icon size={12} />
                                        </span>
                                        <span className="min-w-0 flex-1 truncate" translate="no">{meta.label}</span>
                                    </button>
                                );
                            })}
                        </div>
                    </div>
                ))}
            </div>
        </div>
    );
}
