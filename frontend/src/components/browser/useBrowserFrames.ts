'use client';

import { useEffect, useState } from 'react';

import { actionOf, type BrowserAction, type BrowserFrame } from './model';

/** No frame and no keepalive for this long means the stream died quietly (the component sends one every 15 s). */
const STALL_MS = 45_000;

/**
 * The last frame of each browser, and the person's own lines, kept by whoever
 * outlives the pane: a pane that closes and opens again, or comes back with its
 * tab, starts on the page it left (and a closed browser's last page stays).
 * Frames come several a second, so they are kept here rather than in anyone's
 * React state.
 */
export type FrameMemory = Map<string, { frame: BrowserFrame | null; log: BrowserAction[] }>;

export type FramesState = {
  /** The newest frame: the one the stream sent last, or the one kept from before. */
  frame: BrowserFrame | null;
  /** The browser is closed: the stream said so, or the session is gone (404). */
  closed: boolean;
  /**
   * The lines no agent call carries, which come on the frames stream as `{log: entry}`: the person's own presses
   * and sends, and what the guard kept from leaving (BRW1, V2-CONTRACT).
   */
  log: BrowserAction[];
};

/**
 * The newest frame of a session's screencast while `live`, read from an SSE
 * stream. The first frame comes at once, then one per repaint. Unmounting or
 * `live` going false stops the reading, never the browser. A slow reader only
 * ever sees the newest frame, which is why the state holds one.
 */
export function useBrowserFrames(url: string, live: boolean, memory?: FrameMemory, key = url): FramesState {
  const [state, setState] = useState<FramesState>(() => ({
    frame: memory?.get(key)?.frame ?? null, closed: false, log: memory?.get(key)?.log ?? [] }));

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
          const lines: BrowserAction[] = [];
          for (const block of blocks) {
            const data = block.split('\n').filter(l => l.startsWith('data:')).map(l => l.slice(5).trim()).join('');
            if (!data) continue;
            try {
              const parsed = JSON.parse(data);
              if (parsed?.closed) closed = true;
              else if (parsed?.log && typeof parsed.log === 'object') lines.push(actionOf({ session_id: key, ...parsed.log }));
              else if (typeof parsed?.jpeg_b64 === 'string') newest = parsed as BrowserFrame;
            } catch { /* a torn frame: the next one replaces it */ }
          }
          if (newest || lines.length) {
            const kept = memory?.get(key);
            memory?.set(key, { frame: newest ?? kept?.frame ?? null, log: [...(kept?.log ?? []), ...lines] });
          }
          if (newest || closed || lines.length) {
            setState(s => ({ frame: newest ?? s.frame, closed: s.closed || closed,
                             log: lines.length ? [...s.log, ...lines] : s.log }));
          }
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

/**
 * The whole log kept with a chat or run once its browser closed (`{entries}`), read once; null while it is read,
 * when there is none to read (`url` null), or when it could not be (an older chat or run keeps none), so the caller
 * shows the lines it heard instead.
 */
export function useKeptLog(url: string | null): BrowserAction[] | null {
  const [kept, setKept] = useState<{ url: string; entries: BrowserAction[] } | null>(null);
  useEffect(() => {
    if (!url) return;
    const controller = new AbortController();
    fetch(url, { signal: controller.signal })
      .then(res => (res.ok ? res.json() : null))
      .then((body: { entries?: Record<string, unknown>[] } | null) => {
        if (Array.isArray(body?.entries)) setKept({ url, entries: body.entries.map(e => actionOf(e)) });
      })
      .catch(() => undefined);
    return () => controller.abort();
  }, [url]);
  return url && kept?.url === url ? kept.entries : null;
}
