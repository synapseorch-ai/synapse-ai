'use client';

import { useRef, useState } from 'react';

import { usePersisted } from '@/components/ui';

/** The panel's width, before the person sets one: a browser needs the room (Naveen 11-06, on the mocks: wider). */
const DEFAULT_WIDTH = 'min(1280px, 80vw)';
const MIN_WIDTH = 560;
/** Room kept at the left, so the page it was opened from still shows. */
const GUTTER = 64;

/**
 * A browser of the person's alone (signing in, recording a task, re-recording one), full height on the right, the way
 * the workflow page shows a step (F1428, Naveen 11-06) where it was a popup in the middle of the screen. Full screen on
 * a phone. Its left edge resizes it, and the width is kept.
 *
 * It closes only by the pane's own buttons (Keep, Don't keep, Stop recording, the replay's way out), as the popup did:
 * no Escape and no click beside it, so a sign-in or a recording is never lost to a stray key.
 */
export function BrowserPanel({ label, children }: {
  /** What the browser is for: the dialog's name. */
  label: string;
  /** The pane, at the panel's full height. */
  children: React.ReactNode;
}) {
  const [kept, setKept] = usePersisted<string>('browser-panel-width', raw => (raw && /^\d+$/.test(raw) ? raw : ''), '');
  const [dragged, setDragged] = useState<number | null>(null);
  const widthRef = useRef(0);
  const panel = useRef<HTMLDivElement>(null);
  const clamp = (px: number) => Math.round(Math.min(window.innerWidth - GUTTER, Math.max(MIN_WIDTH, px)));
  const width = dragged ?? (kept ? Number(kept) : null);

  const startResize = (e: React.PointerEvent) => {
    e.preventDefault();
    const startX = e.clientX;
    const startW = panel.current?.getBoundingClientRect().width ?? MIN_WIDTH;
    widthRef.current = startW;
    const onMove = (ev: PointerEvent) => {
      widthRef.current = clamp(startW + (startX - ev.clientX));
      setDragged(widthRef.current);
    };
    const onUp = () => {
      window.removeEventListener('pointermove', onMove);
      setKept(String(widthRef.current));
      setDragged(null);
    };
    window.addEventListener('pointermove', onMove);
    window.addEventListener('pointerup', onUp, { once: true });
  };

  return (
    <div role="dialog" aria-modal="true" aria-label={label} className="fixed inset-0 z-50">
      <div aria-hidden className="absolute inset-0 bg-scrim/40" />
      <div
        ref={panel}
        className="absolute inset-y-0 right-0 flex w-full flex-col border-l border-border bg-surface shadow-elevated md:w-[var(--browser-panel-w)] md:max-w-[calc(100vw-4rem)] md:min-w-[35rem]"
        style={{ '--browser-panel-w': width ? `${width}px` : DEFAULT_WIDTH } as React.CSSProperties}
      >
        <div
          role="separator"
          aria-orientation="vertical"
          aria-label="Resize the browser"
          onPointerDown={startResize}
          className="absolute inset-y-0 -left-0.5 z-10 hidden w-1.5 cursor-col-resize transition-colors hover:bg-accent/40 active:bg-accent/60 md:block"
        />
        {children}
      </div>
    </div>
  );
}
