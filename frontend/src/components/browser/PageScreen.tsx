'use client';

import { useRef } from 'react';

import { cn } from '@/lib/cn';

import type { BrowserFrame, BrowserInput } from './model';
import { pagePoint } from './pagePoint';

/** Keys that are not text: sent as keys, never typed into the page's field as characters. */
const KEYS = new Set([
  'Enter', 'Tab', 'Backspace', 'Delete', 'Escape', 'ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight',
  'Home', 'End', 'PageUp', 'PageDown',
]);

const BUTTONS = ['left', 'middle', 'right'] as const;

function modifiers(e: { shiftKey: boolean; ctrlKey: boolean; altKey: boolean; metaKey: boolean }): string[] {
  return [e.shiftKey && 'Shift', e.ctrlKey && 'Control', e.altKey && 'Alt', e.metaKey && 'Meta'].filter(Boolean) as string[];
}

/**
 * The page itself: the newest frame, and, while the person holds the wheel, their clicks, scrolls, keys and typing
 * sent to it (DESIGN §2.3). A click is placed where it was made on the page: through the picture as drawn inside its
 * letterboxed box, scaled to the page's own size (F1197); a press in a bar beside the picture sends nothing. Typing goes through a hidden field that has the focus, so a phone brings up its keyboard on a tap and its
 * words arrive as text. While the agent drives, a press on the page asks first (`onPressWhileWatching`).
 */
export function PageScreen({ frame, closed, driving, alt, send, onPressWhileWatching }: {
  frame: BrowserFrame;
  closed: boolean;
  driving: boolean;
  alt: string;
  send: (input: BrowserInput) => void;
  onPressWhileWatching: () => void;
}) {
  const img = useRef<HTMLImageElement>(null);
  const keys = useRef<HTMLTextAreaElement>(null);
  const composing = useRef(false);
  const scroll = useRef<{ dx: number; dy: number; x: number; y: number; timer: ReturnType<typeof setTimeout> | null }>(
    { dx: 0, dy: 0, x: 0, y: 0, timer: null });

  const at = (clientX: number, clientY: number) => {
    const el = img.current;
    if (!el) return null;
    const r = el.getBoundingClientRect();
    // The content box: object-contain draws inside the border, not across it.
    const box = { left: r.left + el.clientLeft, top: r.top + el.clientTop, width: el.clientWidth, height: el.clientHeight };
    const picture = { width: el.naturalWidth || frame.w, height: el.naturalHeight || frame.h };
    return pagePoint(box, picture, { width: frame.w, height: frame.h }, { x: clientX, y: clientY });
  };

  const press = (e: React.PointerEvent) => {
    if (closed) return;
    if (!driving) {
      onPressWhileWatching();
      return;
    }
    const p = at(e.clientX, e.clientY);
    if (!p) return;
    e.preventDefault();
    send({ type: 'mouse', event: 'click', ...p, button: BUTTONS[e.button] ?? 'left', clicks: e.detail > 1 ? 2 : 1,
           modifiers: modifiers(e) });
    // Focused inside the press, so a phone's keyboard may open.
    keys.current?.focus({ preventScroll: true });
  };

  const wheel = (e: React.WheelEvent) => {
    if (!driving || closed) return;
    const p = at(e.clientX, e.clientY);
    if (!p) return;
    // One scroll per tenth of a second: a trackpad sends dozens.
    const s = scroll.current;
    s.dx += e.deltaX;
    s.dy += e.deltaY;
    s.x = p.x;
    s.y = p.y;
    if (s.timer) return;
    s.timer = setTimeout(() => {
      send({ type: 'scroll', x: s.x, y: s.y, dx: Math.round(s.dx), dy: Math.round(s.dy) });
      s.dx = s.dy = 0;
      s.timer = null;
    }, 100);
  };

  const key = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (composing.current) return;
    const chord = e.ctrlKey || e.metaKey;
    if (KEYS.has(e.key) || (chord && e.key.length === 1)) {
      e.preventDefault();
      send({ type: 'key', key: e.key, modifiers: modifiers(e) });
    }
  };

  const typed = (e: React.FormEvent<HTMLTextAreaElement>) => {
    if (composing.current) return;
    const text = e.currentTarget.value;
    e.currentTarget.value = '';
    if (text) send({ type: 'text', text });
  };

  return (
    <div
      role="application"
      aria-label={driving ? 'The page: click and type here while you drive' : 'The page, as the agent sees it'}
      onPointerUp={press}
      onWheel={wheel}
      className={cn('relative flex max-h-full max-w-full', driving ? 'cursor-default' : 'cursor-pointer')}
    >
      {/* eslint-disable-next-line @next/next/no-img-element -- a screencast frame, replaced on every repaint */}
      <img
        ref={img}
        src={`data:image/jpeg;base64,${frame.jpeg_b64}`}
        alt={alt}
        width={frame.w || undefined}
        height={frame.h || undefined}
        draggable={false}
        className={cn('h-auto max-h-full w-auto max-w-full select-none rounded-md border object-contain shadow-sm',
          driving ? 'border-person ring-2 ring-person/60' : 'border-border',
          closed && 'opacity-50 grayscale')}
      />
      {driving && !closed && (
        <textarea
          ref={keys}
          aria-label="Type into the page"
          autoCapitalize="off"
          autoCorrect="off"
          spellCheck={false}
          onKeyDown={key}
          onInput={typed}
          onCompositionStart={() => { composing.current = true; }}
          onCompositionEnd={(e) => {
            composing.current = false;
            const text = e.currentTarget.value;
            e.currentTarget.value = '';
            if (text) send({ type: 'text', text });
          }}
          className="pointer-events-none absolute left-0 top-0 size-px resize-none opacity-0"
        />
      )}
    </div>
  );
}
