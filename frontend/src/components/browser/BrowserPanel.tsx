'use client';

import { createContext, useCallback, useEffect, useId, useRef, useState } from 'react';
import { X } from 'lucide-react';

import { IconButton, usePersisted } from '@/components/ui';

/** The panel's width, before the person sets one: a browser needs the room (Naveen 11-06, on the mocks: wider). */
const DEFAULT_WIDTH = 'min(1280px, 80vw)';
const MIN_WIDTH = 560;
/** Room kept at the left, so the page it was opened from still shows. */
const GUTTER = 64;

/**
 * How the pane inside says what its ✕ does (F1439): the pane knows whether anything would be lost (a sign-in typed
 * into, steps recorded) and asks first; the panel only draws the button. A pane registers its close, null to unset.
 */
export const BrowserPanelClose = createContext<((close: (() => void) | null) => void) | null>(null);

/**
 * A browser of the person's alone (signing in, recording a task, re-recording one), full height on the right, the way
 * the workflow page shows a step (F1428, Naveen 11-06) where it was a popup in the middle of the screen. Full screen on
 * a phone. Its left edge resizes it, and the width is kept.
 *
 * It closes by its ✕ (F1439, Naveen 11-06: there was no close to see) or the pane's own buttons (Keep, Don't keep, Stop
 * recording, the replay's way out); the ✕ asks first when something would be lost. No Escape and no click beside it, so
 * a sign-in or a recording is never lost to a stray key. Focus moves in when it opens and back to its opener after.
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
  const titleId = useId();
  const root = useRef<HTMLDivElement>(null);
  const closer = useRef<(() => void) | null>(null);
  const register = useCallback((close: (() => void) | null) => { closer.current = close; }, []);

  // Focus in, and back to what opened it once it closes. What opened it can be a dialog that hands focus back to its
  // own opener as the panel opens ("Keep me signed in to a site"): focus landing on the page behind is that opener,
  // and comes back in. A dialog over the panel (its own ask) keeps its focus.
  useEffect(() => {
    const held = document.activeElement;
    let opener = held instanceof HTMLElement && held !== document.body ? held : null;
    root.current?.focus();
    const onFocus = (e: FocusEvent) => {
      const t = e.target;
      if (!(t instanceof HTMLElement) || root.current?.contains(t) || t.closest('[role="dialog"]')) return;
      opener = t;
      root.current?.focus();
    };
    document.addEventListener('focusin', onFocus);
    return () => {
      document.removeEventListener('focusin', onFocus);
      if (opener?.isConnected) opener.focus();
    };
  }, []);

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
    <div ref={root} role="dialog" aria-modal="true" aria-labelledby={titleId} tabIndex={-1}
         className="fixed inset-0 z-50 focus:outline-none">
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
        <div className="flex shrink-0 items-center gap-2 border-b border-border py-1.5 pl-3 pr-1.5">
          <h2 id={titleId} className="min-w-0 flex-1 truncate text-sm font-semibold text-text">{label}</h2>
          <IconButton icon={X} label="Close" size="sm" onClick={() => closer.current?.()} />
        </div>
        <BrowserPanelClose.Provider value={register}>
          <div className="min-h-0 flex-1">{children}</div>
        </BrowserPanelClose.Provider>
      </div>
    </div>
  );
}
