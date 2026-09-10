'use client';

import { useCallback, useEffect, useRef, useState } from 'react';

import styles from './embed.module.css';

/**
 * The chat a visitor actually sees.
 *
 * Written small and on its own rather than reusing the workspace chat screen,
 * and the reason is worth stating: that screen renders reasoning, tool calls with
 * their arguments, a task checklist and an approval card. Every one of those is
 * a window into how the agent works, shown to a member who is entitled to see
 * it. On a stranger's browser they are an information leak wearing a spinner —
 * the tool arguments alone can carry a customer id or a SQL statement.
 *
 * So this consumes the same event stream and keeps three things from it: whether
 * the agent is working, what it finally said, and whether it failed. `status`
 * events become a single "Thinking…", never their text, for the same reason.
 */

type Message = { role: 'user' | 'assistant'; text: string; failed?: boolean };

const THINKING = 'Thinking…';

export default function WidgetChat({
  token: initialToken,
  storageKey,
  greeting,
  accent,
  title,
}: {
  token: string;
  storageKey: string;
  greeting: string;
  accent: string;
  title: string;
}) {
  const [messages, setMessages] = useState<Message[]>([]);
  const [draft, setDraft] = useState('');
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState('');
  const token = useRef(initialToken);
  const abort = useRef<AbortController | null>(null);
  const log = useRef<HTMLDivElement>(null);
  const lastSent = useRef('');

  useEffect(() => {
    log.current?.scrollTo({ top: log.current.scrollHeight, behavior: 'smooth' });
  }, [messages, status]);

  /** Keep the live token and the stored one in step; storage may refuse. */
  const remember = useCallback(
    (fresh: string) => {
      token.current = fresh;
      try {
        window.sessionStorage.setItem(`synapse:widget:${storageKey}`, fresh);
      } catch {
        /* a browser blocking site data still chats; it just will not resume */
      }
    },
    [storageKey],
  );

  /**
   * Pick the conversation back up when the visitor moves between pages.
   *
   * A bubble widget is reloaded by every navigation on the host site, and a
   * conversation that resets each time is one nobody finishes. What is stored is
   * the whole **token**, not the session id: the id alone would have to be
   * traded for a token, and a session id a client can hand back is one a client
   * can invent. A token is something only we can have signed, so honouring one
   * is safe by construction — and if it has expired the refresh below fails and
   * the freshly minted one takes over.
   *
   * `sessionStorage`, not `localStorage`: this is our own origin inside the
   * frame, so it is invisible to the host page either way, but a conversation
   * should end with the tab rather than outlive it on a shared computer.
   * Wrapped, because a browser set to block site data throws on access.
   */
  useEffect(() => {
    let cancelled = false;
    const slot = `synapse:widget:${storageKey}`;

    const resume = async () => {
      let stored = '';
      try {
        stored = window.sessionStorage.getItem(slot) ?? '';
      } catch {
        return;
      }
      if (!stored) {
        try {
          window.sessionStorage.setItem(slot, initialToken);
        } catch {
          /* nothing to do; the conversation just will not survive a reload */
        }
        return;
      }

      const refreshed = await fetch('/api/widget/refresh', {
        method: 'POST',
        headers: { Authorization: `Bearer ${stored}` },
      }).catch(() => null);

      if (!refreshed?.ok) {
        try {
          window.sessionStorage.setItem(slot, initialToken);
        } catch {
          /* as above */
        }
        return;
      }

      const fresh = (await refreshed.json()).token as string;
      if (cancelled) return;
      remember(fresh);

      const history = await fetch('/api/widget/history', {
        headers: { Authorization: `Bearer ${fresh}` },
      }).catch(() => null);
      if (!history?.ok || cancelled) return;

      const rows = ((await history.json()).messages ?? []) as { role: string; content: string }[];
      setMessages(
        rows
          .filter((row) => row.role === 'user' || row.role === 'assistant')
          .map((row) => ({ role: row.role as 'user' | 'assistant', text: row.content ?? '' })),
      );
    };

    void resume();
    return () => {
      cancelled = true;
    };
  }, [initialToken, storageKey, remember]);

  /**
   * Tell the loader the frame really rendered.
   *
   * This is what makes a blocked widget diagnosable. When an origin is not on
   * the allow-list the *browser* refuses to paint this frame, which is the
   * strongest possible refusal and also a completely silent one — the host page
   * gets a grey box and no explanation. The loader waits for this message and,
   * not hearing it, says out loud what probably happened.
   */
  useEffect(() => {
    window.parent?.postMessage({ type: 'synapse:ready' }, '*');
  }, []);

  const close = () => window.parent?.postMessage({ type: 'synapse:close' }, '*');

  const send = useCallback(async (text: string) => {
    if (!text.trim() || busy) return;
    lastSent.current = text;
    setMessages((m) => [...m, { role: 'user', text }]);
    setDraft('');
    setBusy(true);
    setStatus(THINKING);

    const controller = new AbortController();
    abort.current = controller;

    // One retry, and only for an expired token. A conversation that outlives the
    // token's hour is the ordinary case for a widget somebody leaves open in a
    // tab, and making them reload to keep talking would be a bug they blame on
    // the agent.
    const post = async (auth: string) =>
      fetch('/api/widget/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${auth}` },
        body: JSON.stringify({ message: text }),
        signal: controller.signal,
      });

    try {
      let response = await post(token.current);
      if (response.status === 401) {
        const refreshed = await fetch('/api/widget/refresh', {
          method: 'POST',
          headers: { Authorization: `Bearer ${token.current}` },
        });
        if (refreshed.ok) {
          remember((await refreshed.json()).token);
          response = await post(token.current);
        }
      }

      if (!response.ok || !response.body) {
        const detail = (await response.json().catch(() => ({}))).detail;
        setMessages((m) => [
          ...m,
          { role: 'assistant', text: detail || 'Something went wrong. Please try again.', failed: true },
        ]);
        return;
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      let answered = false;

      // Frames are split on a blank line and a partial one is kept, because a
      // chunk boundary lands mid-frame often enough to matter.
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const frames = buffer.split('\n\n');
        buffer = frames.pop() ?? '';

        for (const frame of frames) {
          const line = frame.split('\n').find((l) => l.startsWith('data: '));
          if (!line) continue;
          let event: { type?: string; response?: string; content?: string; message?: string };
          try {
            event = JSON.parse(line.slice(6));
          } catch {
            continue;
          }

          if (event.type === 'status' || event.type === 'thinking') {
            // The engine's own words describe the machinery. The visitor gets
            // that it is working, and nothing about how.
            setStatus(THINKING);
          } else if (event.type === 'final' || event.type === 'response') {
            answered = true;
            const text2 = event.response ?? event.content ?? '';
            setMessages((m) => [...m, { role: 'assistant', text: text2 }]);
          } else if (event.type === 'cancelled') {
            answered = true;
            setMessages((m) => [...m, { role: 'assistant', text: 'Stopped.', failed: true }]);
          } else if (event.type === 'error' || event.type === 'orchestration_error') {
            answered = true;
            setMessages((m) => [
              ...m,
              { role: 'assistant', text: 'Something went wrong. Please try again.', failed: true },
            ]);
          }
        }
      }

      if (!answered) {
        setMessages((m) => [
          ...m,
          { role: 'assistant', text: 'Something went wrong. Please try again.', failed: true },
        ]);
      }
    } catch (error) {
      // An abort is the Stop button working, not a failure to report.
      if ((error as Error).name !== 'AbortError') {
        setMessages((m) => [
          ...m,
          { role: 'assistant', text: 'Something went wrong. Please try again.', failed: true },
        ]);
      }
    } finally {
      setBusy(false);
      setStatus('');
      abort.current = null;
    }
  }, [busy, remember]);

  /**
   * Two calls, and either alone is half a stop: the abort tells this page to
   * stop listening, the cancel tells the agent to stop working.
   */
  const stop = async () => {
    abort.current?.abort();
    try {
      await fetch('/api/widget/cancel', {
        method: 'POST',
        headers: { Authorization: `Bearer ${token.current}` },
      });
    } catch {
      /* the abort already stopped this side */
    }
  };

  const retry = () => {
    setMessages((m) => {
      const trimmed = [...m];
      while (trimmed.length && trimmed[trimmed.length - 1].role === 'assistant') trimmed.pop();
      if (trimmed.length && trimmed[trimmed.length - 1].role === 'user') trimmed.pop();
      return trimmed;
    });
    void send(lastSent.current);
  };

  const failed = messages.length > 0 && messages[messages.length - 1].failed;

  return (
    <div className={styles.shell} style={{ ['--accent' as string]: accent }}>
      <header className={styles.header}>
        <span className={styles.title}>{title || 'Chat'}</span>
        <button type="button" onClick={close} className={styles.close} aria-label="Close chat">
          ×
        </button>
      </header>

      <div className={styles.log} ref={log} role="log" aria-live="polite">
        <p className={styles.greeting}>{greeting}</p>
        {messages.map((message, index) => (
          <p
            key={index}
            className={`${styles.bubble} ${
              message.role === 'user' ? styles.fromUser : styles.fromAgent
            } ${message.failed ? styles.failed : ''}`}
          >
            {message.text}
          </p>
        ))}
        {status && <p className={styles.status}>{status}</p>}
        {failed && !busy && (
          <button type="button" onClick={retry} className={styles.retry}>
            Try again
          </button>
        )}
      </div>

      <form
        className={styles.composer}
        onSubmit={(event) => {
          event.preventDefault();
          void send(draft);
        }}
      >
        <input
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          placeholder="Type a message…"
          className={styles.input}
          aria-label="Message"
          disabled={busy}
          autoComplete="off"
        />
        {busy ? (
          <button type="button" onClick={stop} className={styles.stop} aria-label="Stop">
            Stop
          </button>
        ) : (
          <button type="submit" className={styles.send} disabled={!draft.trim()}>
            Send
          </button>
        )}
      </form>
    </div>
  );
}
