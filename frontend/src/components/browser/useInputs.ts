'use client';

import { useCallback, useRef, useState } from 'react';

import type { BrowserHost, BrowserInput } from './model';

export type Inputs = {
  /** Sends what the person did to the page: after what they did before, never alongside it. */
  send: (input: BrowserInput) => void;
  /** The browser didn't take the last input, and nothing made after it was sent. Cleared by the next one it takes. */
  failed: boolean;
};

/**
 * The person's input to a browser, sent one at a time in the order it was made: typing must arrive as typed (F1508,
 * eb's QA B4). Text typed while a send is out goes with the next send, joined, so a slow browser (cloud's relay) gets
 * one call per pause rather than one per character; a key, a click or a scroll keeps its place. An input the browser
 * didn't take is said, and what was made after it is not sent: the page never gets half of what was typed.
 */
export function useInputs(host: BrowserHost, sessionId: string): Inputs {
  const waiting = useRef<BrowserInput[]>([]);
  const sending = useRef(false);
  const [failed, setFailed] = useState(false);

  const send = useCallback((input: BrowserInput) => {
    waiting.current.push(input);
    if (sending.current) return;
    sending.current = true;
    void (async () => {
      try {
        while (waiting.current.length) {
          let next = waiting.current.shift() as BrowserInput;
          while (next.type === 'text' && waiting.current[0]?.type === 'text') {
            const more = waiting.current.shift() as Extract<BrowserInput, { type: 'text' }>;
            next = { type: 'text', text: next.text + more.text };
          }
          const took = await (host.input ? host.input(sessionId, next) : Promise.resolve(true)).catch(() => false);
          if (!took) {
            waiting.current = [];
            setFailed(true);
            return;
          }
          setFailed(false);
        }
      } finally {
        sending.current = false;
      }
    })();
  }, [host, sessionId]);

  return { send, failed };
}
