'use client';
/**
 * The canvas's two bottom clusters (Prototype 4): the dock in the middle (Add
 * step, Tidy up, Undo, Redo) and the view at the right (the minimap, zoom, Fit).
 */
import { Map as MapIcon, Maximize, Minus, Plus, Redo2, Undo2, Wand2 } from 'lucide-react';
import { useViewport } from '@xyflow/react';
import { Button, IconButton, buttonStyles, useCommandShortcutLabel } from '@/components/ui';
import { cn } from '@/lib/cn';
import { FLOAT } from './TopBar';
import type { Tier } from './geometry';

export function Dock({
    left, phone, tier, onAddStep, onTidy, tidy, undo, redo, canUndo, canRedo,
}: {
    /** The dock's centre, in the canvas's pixels. */
    left: number;
    phone: boolean;
    tier: Tier;
    onAddStep: () => void;
    onTidy: () => void;
    tidy: boolean;
    undo: () => void;
    redo: () => void;
    canUndo: boolean;
    canRedo: boolean;
}) {
    const shortcut = useCommandShortcutLabel();
    const words = tier === 'full' && !phone;
    return (
        <div
            role="toolbar"
            aria-label="Canvas tools"
            data-canvas-chrome="dock"
            className={cn(FLOAT, 'nokey absolute bottom-2 z-10 flex -translate-x-1/2 items-center gap-0.5 p-1 md:bottom-3')}
            style={{ left }}
        >
            <Button size="sm" variant="ghost" onClick={onAddStep} aria-label="Add step" title={`Add step (${shortcut})`} className="text-text">
                <Plus className="size-4" aria-hidden />
                {words && (
                    <>
                        Add step
                        <kbd className="rounded border border-border px-1 font-mono text-2xs text-text-faint">{shortcut}</kbd>
                    </>
                )}
            </Button>
            {tidy && (
                <Button size="sm" variant="ghost" onClick={onTidy} aria-label={words ? undefined : 'Tidy up'} title="Auto-arrange the graph left to right" className="text-text">
                    <Wand2 className="size-3.5" aria-hidden />
                    {words && 'Tidy up'}
                </Button>
            )}
            <span className="mx-1 h-5 w-px bg-border" aria-hidden />
            <IconButton label="Undo (Ctrl+Z)" icon={Undo2} size="sm" onClick={undo} disabled={!canUndo} />
            <IconButton label="Redo (Ctrl+Shift+Z)" icon={Redo2} size="sm" onClick={redo} disabled={!canRedo} />
        </div>
    );
}

export function ZoomCluster({
    right, tier, minimap, onMinimap, zoomIn, zoomOut, zoomTo, fit,
}: {
    right: number;
    tier: Tier;
    minimap: boolean;
    onMinimap: (on: boolean) => void;
    zoomIn: () => void;
    zoomOut: () => void;
    zoomTo: (zoom: number) => void;
    fit: () => void;
}) {
    const { zoom } = useViewport();
    const tight = tier === 'tight';
    return (
        <div
            data-canvas-chrome="zoom"
            className={cn(FLOAT, 'nokey absolute bottom-2 z-10 flex items-center gap-0.5 p-1 md:bottom-3')}
            style={{ right }}
        >
            {!tight && (
                <>
                    <button
                        type="button"
                        aria-label="Minimap"
                        aria-pressed={minimap}
                        title={minimap ? 'Hide the minimap' : 'Show the minimap'}
                        onClick={() => onMinimap(!minimap)}
                        className={cn(
                            buttonStyles({ variant: 'ghost', size: 'sm', iconOnly: true }),
                            minimap && 'bg-surface-2 text-text shadow-[inset_0_0_0_1px_var(--border-strong)]',
                        )}
                    >
                        <MapIcon className="size-4" aria-hidden />
                    </button>
                    <span className="mx-1 h-5 w-px bg-border" aria-hidden />
                    <IconButton label="Zoom out" icon={Minus} size="sm" onClick={zoomOut} />
                    <button
                        type="button"
                        onClick={() => zoomTo(1)}
                        title="Zoom to 100%"
                        aria-label={`Zoom ${Math.round(zoom * 100)}%, press for 100%`}
                        className={cn(buttonStyles({ variant: 'ghost', size: 'sm' }), 'min-w-[3.25rem] justify-center px-1 font-mono text-xs tabular-nums text-text-muted')}
                    >
                        {Math.round(zoom * 100)}%
                    </button>
                    <IconButton label="Zoom in" icon={Plus} size="sm" onClick={zoomIn} />
                </>
            )}
            <button
                type="button"
                onClick={fit}
                aria-label="Fit view"
                title="Fit the whole graph in view"
                className={buttonStyles({ variant: 'ghost', size: 'sm', iconOnly: true })}
            >
                <Maximize className="size-4" aria-hidden />
            </button>
        </div>
    );
}
