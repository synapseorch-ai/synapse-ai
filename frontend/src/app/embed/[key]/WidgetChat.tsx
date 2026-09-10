'use client';

import React, { useCallback, useEffect, useRef, useState } from 'react';

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

/** Everything the customer chose about how this looks. */
export type Theme = {
  accent: string;
  on_accent: string;
  font: string;
  theme: string;
  header_style: string;
  launcher: string;
  launcher_label: string;
  logo: string;
  title: string;
  greeting: string;
};

export default function WidgetChat({
  token: initialToken,
  storageKey,
  theme: initialTheme,
  preview = false,
}: {
  token: string;
  storageKey: string;
  theme: Theme;
  /**
   * Only true on the owner's own preview route.
   *
   * It does two things, and both must stay off in the real widget: it lets the
   * parent push a draft theme in by `postMessage`, and it skips the transcript,
   * so previewing does not resume whatever conversation a visitor last had.
   */
  preview?: boolean;
}) {
  const [theme, setTheme] = useState(initialTheme);
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
   * Let the settings screen drive this frame while somebody is editing.
   *
   * The preview renders the *real* widget rather than a mock — a mock drifts
   * from the thing it previews, and then it is worse than nothing — so the only
   * way to show an unsaved change is to hand it to the real one.
   *
   * Guarded twice: `preview` is false everywhere a visitor can reach, and the
   * origin check refuses anything not sent by our own app. On a customer's site
   * the parent is a different origin and could not pass either.
   */
  useEffect(() => {
    if (!preview) return;
    const onMessage = (event: MessageEvent) => {
      if (event.origin !== window.location.origin) return;
      const data = event.data as { type?: string; theme?: Theme } | null;
      if (data?.type === 'synapse:preview-theme' && data.theme) setTheme(data.theme);
    };
    window.addEventListener('message', onMessage);
    // The parent may have rendered first and already sent one.
    window.parent?.postMessage({ type: 'synapse:preview-ready' }, window.location.origin);
    return () => window.removeEventListener('message', onMessage);
  }, [preview]);

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
    if (preview) return;
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
  }, [initialToken, storageKey, remember, preview]);

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
    // The launcher's own appearance travels with this message.
    //
    // The floating button lives in the *host* page, where it can be positioned
    // against the viewport, but everything it should look like is decided here.
    // Sending it up costs nothing — this frame is already loading — where a
    // second endpoint for the loader to fetch would be another public surface
    // and another round trip before the button paints.
    window.parent?.postMessage(
      {
        type: 'synapse:ready',
        launcher: {
          accent: theme.accent,
          onAccent: theme.on_accent,
          logo: theme.launcher === 'logo' ? theme.logo : '',
          label: theme.launcher_label,
        },
      },
      '*',
    );
  }, [theme]);

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

      /**
       * Where the events actually are, which differs by deployment.
       *
       * Self-hosted runs the turn in the request and streams straight back from
       * this POST. The hosted product queues every turn onto a shared fleet, so
       * the POST can only answer "accepted" with a cursor, and the events come
       * off a stream opened separately. Branching on the content type we were
       * actually handed is the honest way to serve both from one file — the
       * alternative is a build flag that lies on whichever product it is wrong
       * about.
       */
      let stream = response;
      if (!(response.headers.get('content-type') ?? '').includes('text/event-stream')) {
        const accepted = (await response.json().catch(() => ({}))) as { cursor?: string };
        const opened = await fetch(
          `/api/widget/stream?cursor=${encodeURIComponent(accepted.cursor ?? '0')}`,
          { headers: { Authorization: `Bearer ${token.current}` }, signal: controller.signal },
        );
        if (!opened.ok || !opened.body) {
          setMessages((m) => [
            ...m,
            { role: 'assistant', text: 'Something went wrong. Please try again.', failed: true },
          ]);
          return;
        }
        stream = opened;
      }

      const reader = stream.body!.getReader();
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
          } else if (event.type === 'done') {
            // The queued stream stays open until it is told the turn is over.
            await reader.cancel().catch(() => {});
            break;
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
    <div
      className={styles.shell}
      data-font={theme.font}
      data-theme={theme.theme}
      data-header={theme.header_style}
      style={
        {
          '--accent': theme.accent,
          '--on-accent': theme.on_accent,
        } as React.CSSProperties
      }
    >
      <header className={styles.header}>
        {theme.logo && (
          // A plain `<img>`, not `next/image`: the logo is already a `data:`
          // URI inlined into this page, so there is nothing to fetch, resize or
          // cache — the optimizer would only add a dependency to a page served
          // to every visitor of every customer site.
          //
          // `alt=""` is deliberate: the title beside it already names the
          // business, and a screen reader announcing "Acme logo, Acme" is worse
          // than silence.
          // eslint-disable-next-line @next/next/no-img-element
          <img src={theme.logo} alt="" className={styles.logo} />
        )}
        <span className={styles.title}>{theme.title || 'Chat'}</span>
        <button type="button" onClick={close} className={styles.close} aria-label="Close chat">
          ×
        </button>
      </header>

      <div className={styles.log} ref={log} role="log" aria-live="polite">
        <p className={styles.greeting}>{theme.greeting}</p>
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
