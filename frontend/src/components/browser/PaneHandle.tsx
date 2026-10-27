'use client';

import { useRef } from 'react';

/** The pane's share of its row: Naveen's 60% to start (2026-10-27), and never so wide the chat cannot be read. */
export const PANE_SHARE = { min: 0.3, start: 0.6, max: 0.75 } as const;

/** A kept share, or the start when there is none or it is out of bounds. */
export function parseShare(raw: string | null): string {
  const n = Number(raw);
  return Number.isFinite(n) && n >= PANE_SHARE.min && n <= PANE_SHARE.max ? String(n) : String(PANE_SHARE.start);
}

const clamp = (n: number) => Math.min(PANE_SHARE.max, Math.max(PANE_SHARE.min, n));

/**
 * The left edge of a pane that sits at the right of its row: drag it, or focus it
 * and use the arrow keys. `onShare` hears each move, `onDone` the share to keep.
 * After StepConfigPanel's handle, with a keyboard and a value a screen reader can say.
 */
export function PaneHandle({ share, onShare, onDone, label }: {
  share: number;
  onShare: (share: number) => void;
  onDone: (share: number) => void;
  label: string;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const last = useRef(share);

  const start = (e: React.PointerEvent) => {
    const pane = ref.current?.parentElement;
    const row = pane?.parentElement;
    if (!pane || !row) return;
    e.preventDefault();
    last.current = share;
    const right = pane.getBoundingClientRect().right;
    const width = row.getBoundingClientRect().width;
    const move = (ev: PointerEvent) => {
      last.current = clamp((right - ev.clientX) / width);
      onShare(last.current);
    };
    const up = () => {
      window.removeEventListener('pointermove', move);
      onDone(last.current);
    };
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up, { once: true });
  };

  const key = (e: React.KeyboardEvent) => {
    const step = e.key === 'ArrowLeft' ? 0.02 : e.key === 'ArrowRight' ? -0.02 : 0;
    if (!step) return;
    e.preventDefault();
    const next = clamp(share + step);
    onShare(next);
    onDone(next);
  };

  return (
    <div
      ref={ref}
      role="separator"
      aria-orientation="vertical"
      aria-label={label}
      aria-valuemin={Math.round(PANE_SHARE.min * 100)}
      aria-valuemax={Math.round(PANE_SHARE.max * 100)}
      aria-valuenow={Math.round(share * 100)}
      tabIndex={0}
      onPointerDown={start}
      onKeyDown={key}
      className="absolute inset-y-0 -left-0.5 z-10 w-1.5 cursor-col-resize outline-none transition-colors hover:bg-accent/40 focus-visible:bg-accent/60 active:bg-accent/60"
    />
  );
}
