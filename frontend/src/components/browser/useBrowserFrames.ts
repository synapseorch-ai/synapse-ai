'use client';

import { useEffect, useState } from 'react';

import type { BrowserFrame } from './model';

/** No frame and no keepalive for this long means the stream died quietly (the component sends one every 15 s). */
const STALL_MS = 45_000;

/**
 * The last frame of each browser, kept by whoever outlives the pane: a pane that
 * closes and opens again, or comes back with its tab, starts on the page it left
 * (and a closed browser's last page stays). Frames come several a second, so they
 * are kept here rather than in anyone's React state.
 */
export type FrameMemory = Map<string, BrowserFrame>;

export type FramesState = {
  /** The newest frame: the one the stream sent last, or the one kept from before. */
  frame: BrowserFrame | null;
  /** The browser is closed: the stream said so, or the session is gone (404). */
  closed: boolean;
};

/**
 * The newest frame of a session's screencast while `live`, read from an SSE
 * stream. The first frame comes at once, then one per repaint. Unmounting or
 * `live` going false stops the reading, never the browser. A slow reader only
 * ever sees the newest frame, which is why the state holds one.
 */
export function useBrowserFrames(url: string, live: boolean, memory?: FrameMemory, key = url): FramesState {
  const [state, setState] = useState<FramesState>(() => ({ frame: memory?.get(key) ?? null, closed: false }));

  useEffect(() => {
    if (!live) return;
    const controller = new AbortController();
    let stall: ReturnType<typeof setTimeout> | undefined;
    const arm = () => {
      clearTimeout(stall);
      stall = setTimeout(() => controller.abort('stall'), STALL_MS);
    };
    (async () => {
      let res: Response;
      try {
        res = await fetch(url, { signal: controller.signal, headers: { Accept: 'text/event-stream' } });
      } catch {
        return;
      }
      if (res.status === 404) {
        setState(s => ({ ...s, closed: true }));
        return;
      }
      if (!res.ok || !res.body) return;
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      arm();
      try {
        for (;;) {
          const { done, value } = await reader.read();
          if (done) break;
          arm();
          buffer += decoder.decode(value, { stream: true });
          const blocks = buffer.split('\n\n');
          buffer = blocks.pop() ?? '';
          let newest: BrowserFrame | null = null;
          let closed = false;
          for (const block of blocks) {
            const data = block.split('\n').filter(l => l.startsWith('data:')).map(l => l.slice(5).trim()).join('');
            if (!data) continue;
            try {
              const parsed = JSON.parse(data);
              if (parsed?.closed) closed = true;
              else if (typeof parsed?.jpeg_b64 === 'string') newest = parsed as BrowserFrame;
            } catch { /* a torn frame: the next one replaces it */ }
          }
          if (newest) memory?.set(key, newest);
          if (newest || closed) setState(s => ({ frame: newest ?? s.frame, closed: s.closed || closed }));
          if (closed) break;
        }
      } catch { /* aborted, or the stream broke: the pane keeps its last frame */ }
      finally {
        clearTimeout(stall);
      }
    })();
    return () => {
      clearTimeout(stall);
      controller.abort('hidden');
    };
  }, [url, live, memory, key]);

  return state;
}
