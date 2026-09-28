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

/** Said once the owner has pointed this widget at another agent (F278). */
const UPDATED_NOTICE = 'This chat was updated, so a new conversation has started.';

/**
 * The 409 that means "this conversation's agent is not the widget's any more"
 * (F278) — told from the other 409, a reply still being written, by its code.
 */
async function isUpdated(response: Response): Promise<boolean> {
  if (response.status !== 409) return false;
  const body = (await response.clone().json().catch(() => ({}))) as { code?: string };
  return body.code === 'widget_updated';
}

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
  const [notice, setNotice] = useState('');
  const token = useRef(initialToken);
  const abort = useRef<AbortController | null>(null);
  const log = useRef<HTMLDivElement>(null);
  const lastSent = useRef('');
  /**
   * The cursor of the last event this frame handled. A turn runs on whether or
   * not this frame is reading (F49): when its stream breaks, or the visitor
   * clicks to another page on the host site and this frame reloads, the turn is
   * read again from here rather than sent again.
   */
  const cursor = useRef('0');

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
        // The token this page was just given is for the widget as it is now.
        // A stored one for an agent the widget no longer answers with used to be
        // refreshed and win, so reloading never got a visitor out (F278).
        const updated = refreshed ? await isUpdated(refreshed) : false;
        try {
          window.sessionStorage.setItem(slot, initialToken);
        } catch {
          /* as above */
        }
        if (updated && !cancelled) setNotice(UPDATED_NOTICE);
        return;
      }

      const fresh = (await refreshed.json()).token as string;
      if (cancelled) return;
      remember(fresh);

      const history = await fetch('/api/widget/history', {
        headers: { Authorization: `Bearer ${fresh}` },
      }).catch(() => null);
      if (history && !cancelled && (await isUpdated(history))) {
        remember(initialToken);
        setNotice(UPDATED_NOTICE);
        return;
      }
      if (!history?.ok || cancelled) return;

      const body = (await history.json()) as {
        messages?: { role: string; content: string }[];
        running?: { message: string; cursor: string } | null;
      };
      const rows = body.messages ?? [];
      const restored: Message[] = rows
        .filter((row) => row.role === 'user' || row.role === 'assistant')
        .map((row) => ({ role: row.role as 'user' | 'assistant', text: row.content ?? '' }));
      // A reply still being written is not in the history yet — it is saved when
      // it ends. Show the question and follow the reply from its start.
      if (body.running) restored.push({ role: 'user', text: body.running.message });
      setMessages(restored);
      if (body.running && !cancelled) {
        lastSent.current = body.running.message;
        void watch(body.running.cursor);
      }
    };

    void resume();
    return () => {
      cancelled = true;
    };
    // `watch` reads only refs and state setters; adding it would re-run the
    // resume — and follow the reply twice — on every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
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

  /**
   * Read one turn's events into the conversation, keeping its cursor.
   *
   * 'done' when the turn said so; 'ended' when the stream closed first — a
   * proxy that gave up, a reload — which means read it again, not that it is
   * over. Frames are split on a blank line and a partial one is kept, because a
   * chunk boundary lands mid-frame often enough to matter.
   */
  const consume = async (stream: Response, seen: { answered: boolean }): Promise<'done' | 'ended'> => {
    const reader = stream.body!.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    for (;;) {
      const { done, value } = await reader.read();
      if (done) return 'ended';
      buffer += decoder.decode(value, { stream: true });
      const frames = buffer.split('\n\n');
      buffer = frames.pop() ?? '';

      for (const frame of frames) {
        const lines = frame.split('\n');
        const line = lines.find((l) => l.startsWith('data: '));
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
          seen.answered = true;
          const text2 = event.response ?? event.content ?? '';
          setMessages((m) => [...m, { role: 'assistant', text: text2 }]);
        } else if (event.type === 'cancelled') {
          seen.answered = true;
          setMessages((m) => [...m, { role: 'assistant', text: 'Stopped.', failed: true }]);
        } else if (event.type === 'done') {
          // The queued stream stays open until it is told the turn is over.
          await reader.cancel().catch(() => {});
          return 'done';
        } else if (event.type === 'error' || event.type === 'orchestration_error') {
          seen.answered = true;
          setMessages((m) => [
            ...m,
            { role: 'assistant', text: 'Something went wrong. Please try again.', failed: true },
          ]);
        }
        // The cursor moves once its event is handled, so reading again never
        // skips the event a break interrupted.
        const id = lines.find((l) => l.startsWith('id: '));
        if (id) cursor.current = id.slice(4);
      }
    }
  };

  /**
   * Read this conversation's turn again, from the last event handled.
   * 'gone' when there is none to read — it ended, and its answer is in the history.
   */
  const follow = async (
    controller: AbortController, seen: { answered: boolean },
  ): Promise<'done' | 'ended' | 'gone' | 'updated'> => {
    const opened = await fetch(`/api/widget/stream?cursor=${encodeURIComponent(cursor.current)}`, {
      headers: { Authorization: `Bearer ${token.current}` },
      signal: controller.signal,
    });
    if (opened.status === 404 || opened.status === 410) return 'gone';
    if (await isUpdated(opened)) return 'updated';
    if (!opened.ok || !opened.body) return 'ended';
    return consume(opened, seen);
  };

  /** The reply ended while nobody was reading: take it from the history. */
  const fromHistory = async (seen: { answered: boolean }) => {
    const history = await fetch('/api/widget/history', {
      headers: { Authorization: `Bearer ${token.current}` },
    }).catch(() => null);
    if (!history?.ok) return;
    const rows = ((await history.json()).messages ?? []) as { role: string; content: string }[];
    const last = rows[rows.length - 1];
    const asked = rows[rows.length - 2];
    if (last?.role === 'assistant' && asked?.role === 'user' && asked.content === lastSent.current) {
      seen.answered = true;
      setMessages((m) => [...m, { role: 'assistant', text: last.content ?? '' }]);
    }
  };

  /**
   * A new conversation, with the agent the widget answers with now (F278).
   *
   * The owner pointed the widget at another agent while this conversation was
   * open. Every call on it is refused from then on, and the frame used to show
   * nothing — the typed message vanished, and reloading brought the same stale
   * token back. `keep` is what the visitor had just typed; it goes back in the
   * box to send to the new conversation, never sent by itself.
   */
  const startOver = async (keep: string) => {
    const restarted = await fetch('/api/widget/restart', {
      method: 'POST',
      headers: { Authorization: `Bearer ${token.current}` },
    }).catch(() => null);
    if (!restarted?.ok) {
      const detail = restarted ? (await restarted.json().catch(() => ({}))).detail : '';
      setMessages((m) => [
        ...m,
        { role: 'assistant', text: detail || 'This chat has been updated. Reload the page.', failed: true },
      ]);
      return;
    }
    remember((await restarted.json()).token);
    cursor.current = '0';
    setMessages([]);
    setNotice(UPDATED_NOTICE);
    if (keep) setDraft(keep);
  };

  /**
   * Keep reading a turn whose stream broke. It was never the stream's to lose:
   * the turn runs on, so a few tries while the network comes back, and never
   * sending the question again.
   */
  const recover = async (controller: AbortController, seen: { answered: boolean }) => {
    for (const wait of [0, 1000, 2000, 4000]) {
      if (wait) await new Promise((resolve) => setTimeout(resolve, wait));
      if (controller.signal.aborted) return;
      try {
        const how = await follow(controller, seen);
        if (how === 'done') return;
        if (how === 'gone') return fromHistory(seen);
        if (how === 'updated') {
          seen.answered = true;
          return startOver('');
        }
      } catch (error) {
        if (controller.signal.aborted) return;
        console.error('[widget] reading the reply again failed', error);
      }
    }
  };

  /** Follow a reply this frame did not start — found running when it reloaded. */
  const watch = async (from: string) => {
    const controller = new AbortController();
    abort.current = controller;
    cursor.current = from;
    setBusy(true);
    setStatus(THINKING);
    const seen = { answered: false };
    try {
      const how = await follow(controller, seen);
      if (how === 'gone') await fromHistory(seen);
      else if (how === 'ended') await recover(controller, seen);
      else if (how === 'updated') await startOver('');
    } catch {
      if (!controller.signal.aborted) await recover(controller, seen);
    } finally {
      setBusy(false);
      setStatus('');
      abort.current = null;
    }
  };

  const send = useCallback(async (text: string) => {
    if (!text.trim() || busy) return;
    lastSent.current = text;
    cursor.current = '0';
    setMessages((m) => [...m, { role: 'user', text }]);
    setDraft('');
    setBusy(true);
    setStatus(THINKING);

    const controller = new AbortController();
    abort.current = controller;
    const seen = { answered: false };

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

      if (await isUpdated(response)) {
        // The owner changed the agent (F278): a new conversation, with what
        // was just typed back in the box to send to it.
        setMessages((m) => (m[m.length - 1]?.role === 'user' ? m.slice(0, -1) : m));
        seen.answered = true;
        await startOver(text);
        return;
      }

      if (response.status === 409) {
        // A reply is already being written in this conversation — this frame
        // lost track of it (another tab, a reload mid-turn). Follow that one;
        // what was just typed goes back in the box rather than over it.
        setMessages((m) => (m[m.length - 1]?.role === 'user' ? m.slice(0, -1) : m));
        setDraft(text);
        await recover(controller, seen);
        seen.answered = true;
        return;
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
       * Self-hosted streams the turn back from this POST. The hosted product
       * queues every turn onto a shared fleet, so the POST can only answer
       * "accepted" with a cursor, and the events come off a stream opened
       * separately. Branching on the content type we were actually handed is the
       * honest way to serve both from one file — the alternative is a build flag
       * that lies on whichever product it is wrong about.
       */
      let how: 'done' | 'ended' | 'gone' | 'updated';
      if ((response.headers.get('content-type') ?? '').includes('text/event-stream')) {
        how = await consume(response, seen);
      } else {
        const accepted = (await response.json().catch(() => ({}))) as { cursor?: string };
        cursor.current = accepted.cursor ?? '0';
        how = await follow(controller, seen);
      }
      if (how === 'ended') await recover(controller, seen);
      else if (how === 'gone') await fromHistory(seen);
      else if (how === 'updated') {
        seen.answered = true;
        await startOver(text);
      }
    } catch (error) {
      // An abort is the Stop button working, not a failure to report. Anything
      // else broke the stream, not the turn.
      if (!controller.signal.aborted) {
        console.error('[widget] the reply stream broke — reading it again', error);
        await recover(controller, seen);
      }
    } finally {
      if (!seen.answered && !controller.signal.aborted) {
        setMessages((m) => [
          ...m,
          { role: 'assistant', text: 'Something went wrong. Please try again.', failed: true },
        ]);
      }
      setBusy(false);
      setStatus('');
      abort.current = null;
    }
    // `consume`, `follow` and `recover` read only refs and state setters.
    // eslint-disable-next-line react-hooks/exhaustive-deps
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
        {notice && (
          <p className={styles.notice} role="status">
            {notice}
          </p>
        )}
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
