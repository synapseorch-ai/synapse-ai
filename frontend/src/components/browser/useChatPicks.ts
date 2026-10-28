'use client';

import { useCallback, useEffect, useState } from 'react';

import { listSessions, type SessionsHost, type SiteSession } from './sessions';

/**
 * One chat's own sessions (F1140; DESIGN v5.3): the library's rows, the chat's picks (`GET …/browser-sessions`), and
 * a pick saved as it is made (`PUT {ids}`). A pick the server refuses (422: two on one site) is put back and said.
 * Read once something shows them (`active`), so a chat with no browser reads nothing.
 */
export function useChatPicks(host: SessionsHost, chatId: string | null | undefined, active: boolean) {
  const [sessions, setSessions] = useState<SiteSession[] | null>(null);
  const [value, setValue] = useState<string[]>([]);
  const [failed, setFailed] = useState<string | null>(null);

  useEffect(() => {
    if (!active || !chatId || host.locked) return;
    let gone = false;
    listSessions(host).then(rows => { if (!gone) setSessions(rows); }).catch(() => { if (!gone) setSessions([]); });
    fetch(host.chatPicksUrl(chatId))
      .then(res => (res.ok ? res.json() : { ids: [] }))
      .then(body => { if (!gone) setValue(Array.isArray(body?.ids) ? body.ids : []); })
      .catch(() => {});
    return () => { gone = true; };
  }, [host, chatId, active]);

  const onChange = useCallback(async (ids: string[]) => {
    if (!chatId) return;
    const before = value;
    setValue(ids);
    setFailed(null);
    const res = await fetch(host.chatPicksUrl(chatId), {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ids }),
    }).catch(() => null);
    if (res?.ok) return;
    const body = await res?.json().catch(() => ({}));
    setValue(before);
    setFailed(typeof body?.detail === 'string' ? body.detail : 'That pick was not saved. Try again.');
  }, [host, chatId, value]);

  return { sessions, value, onChange, failed, libraryHref: host.libraryHref, locked: host.locked };
}
