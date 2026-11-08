'use client';
/**
 * The floating setup card (Prototype 4): a step's whole setup, or the
 * workflow's settings, beside the graph instead of in a docked column. Its
 * left edge resizes it (300–560 px, remembered); on a phone it is a bottom
 * sheet that a drag down on its grip puts away.
 */
import { useState } from 'react';
import { CARD_MAX, CARD_MIN } from './geometry';

export function SetupCard({
    width, onWidth, onWidthDone, phone, insetRight, onClose, sheetRef, children,
}: {
    width: number;
    onWidth: (width: number) => void;
    /** The drag or the key ended: the width to remember. */
    onWidthDone: (width: number) => void;
    phone: boolean;
    insetRight: number;
    onClose: () => void;
    sheetRef: React.RefObject<HTMLDivElement | null>;
    children: React.ReactNode;
}) {
    const clamp = (n: number) => Math.min(CARD_MAX, Math.max(CARD_MIN, Math.round(n)));

    const startResize = (event: React.PointerEvent) => {
        event.preventDefault();
        const startX = event.clientX;
        const startW = width;
        let last = startW;
        const onMove = (e: PointerEvent) => {
            last = clamp(startW + (startX - e.clientX));
            onWidth(last);
        };
        const onUp = () => {
            window.removeEventListener('pointermove', onMove);
            onWidthDone(last);
        };
        window.addEventListener('pointermove', onMove);
        window.addEventListener('pointerup', onUp, { once: true });
    };
    const onResizeKey = (event: React.KeyboardEvent) => {
        const step = event.key === 'ArrowLeft' ? 16 : event.key === 'ArrowRight' ? -16 : 0;
        if (!step) return;
        event.preventDefault();
        const next = clamp(width + step);
        onWidth(next);
        onWidthDone(next);
    };

    // A phone's sheet goes away when its grip is dragged down far enough.
    const [drag, setDrag] = useState(0);
    const startSheetDrag = (event: React.PointerEvent) => {
        const startY = event.clientY;
        let dy = 0;
        const onMove = (e: PointerEvent) => {
            dy = Math.max(0, e.clientY - startY);
            setDrag(dy);
        };
        const onUp = () => {
            window.removeEventListener('pointermove', onMove);
            setDrag(0);
            if (dy > 80) onClose();
        };
        window.addEventListener('pointermove', onMove);
        window.addEventListener('pointerup', onUp, { once: true });
    };

    if (phone) {
        return (
            <div
                ref={sheetRef}
                data-canvas-chrome="card"
                className="nokey absolute inset-x-0 bottom-0 z-30 flex max-h-[70%] flex-col overflow-hidden rounded-t-2xl border border-b-0 border-border bg-surface shadow-elevated"
                style={drag ? { transform: `translateY(${drag}px)` } : undefined}
            >
                <button
                    type="button"
                    aria-label="Drag down to close, or press to close"
                    onPointerDown={startSheetDrag}
                    onClick={() => { if (!drag) onClose(); }}
                    className="flex h-5 shrink-0 cursor-grab touch-none items-center justify-center"
                >
                    <span className="h-1 w-9 rounded-full bg-border-strong" aria-hidden />
                </button>
                <div className="flex min-h-0 flex-1 flex-col pb-[env(safe-area-inset-bottom)]">{children}</div>
            </div>
        );
    }

    return (
        <div
            data-canvas-chrome="card"
            className="nokey absolute bottom-3 top-[66px] z-20 flex flex-col overflow-hidden rounded-[14px] border border-border bg-surface shadow-elevated"
            style={{ width, right: 12 + insetRight }}
        >
            <div
                role="separator"
                aria-orientation="vertical"
                aria-label="Resize the panel"
                aria-valuemin={CARD_MIN}
                aria-valuemax={CARD_MAX}
                aria-valuenow={width}
                tabIndex={0}
                onPointerDown={startResize}
                onKeyDown={onResizeKey}
                className="group absolute inset-y-4 left-0 z-10 flex w-2.5 cursor-ew-resize items-center justify-center outline-none"
            >
                <span className="h-9 w-[3px] rounded-full bg-border-strong opacity-0 transition-opacity group-hover:opacity-100 group-focus-visible:opacity-100 group-active:opacity-100" aria-hidden />
            </div>
            {children}
        </div>
    );
}
