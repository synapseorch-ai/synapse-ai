"use client";

/*
 * How each open tab's conversation stands, from one short request (TABS1, F925).
 *
 * Only the tab in front streams: plain HTTP gives a page about six connections
 * per host, so ten tabs each holding one would stall it. The rest are read
 * together: at once when another tab comes to the front, every few seconds
 * while one of them is working, every ten otherwise — a question waiting on
 * the person shouldn't wait long to say so — and not at all while the browser
 * tab is hidden. Each read
 * is compared with the last, and what a tab behind earned in between (done,
 * unread) is handed to `onMark`.
 */

import * as React from "react";

import { markBetween, type Tab, type TabMark, type TabStatus } from "./model";

const BUSY_EVERY = 4_000;
const IDLE_EVERY = 10_000;

export function useTabStatus<T extends Tab>({
  tabs,
  activeId,
  read,
  onMark,
}: {
  tabs: T[];
  activeId: string | null;
  /** One request for all of them; a tab missing from the answer keeps its last status. */
  read: (tabs: T[]) => Promise<Record<string, TabStatus>>;
  onMark: (tabId: string, mark: TabMark) => void;
}): { status: Record<string, TabStatus>; loaded: boolean; refresh: () => void } {
  const [status, setStatus] = React.useState<Record<string, TabStatus>>({});
  const [loaded, setLoaded] = React.useState(false);
  const last = React.useRef<Record<string, TabStatus>>({});
  const latest = React.useRef({ tabs, activeId, read, onMark });
  React.useEffect(() => { latest.current = { tabs, activeId, read, onMark }; });
  const [nudge, setNudge] = React.useState(0);

  // Which tabs there are, and which is in front: a read starts again when one opens, closes or comes forward.
  const ids = `${tabs.map((t) => t.id).join("|")}>${activeId ?? ""}`;

  React.useEffect(() => {
    let live = true;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const tick = async () => {
      const { tabs: now, read: readNow } = latest.current;
      if (now.length === 0) return;
      if (document.visibilityState === "hidden") { timer = setTimeout(tick, IDLE_EVERY); return; }
      let next: Record<string, TabStatus> = {};
      try { next = await readNow(now); } catch { /* the next read tries again */ }
      if (!live) return;
      const { activeId: front, onMark: mark } = latest.current;
      for (const tab of now) {
        const after = next[tab.id];
        if (!after) continue;
        const awaiting = (tab as { awaiting?: boolean }).awaiting === true;
        const earned = markBetween(last.current[tab.id], after, tab.id === front, awaiting);
        if (earned) mark(tab.id, earned);
      }
      last.current = { ...last.current, ...next };
      setStatus(last.current);
      setLoaded(true);
      const busy = Object.values(last.current).some((s) => s.running);
      timer = setTimeout(tick, busy ? BUSY_EVERY : IDLE_EVERY);
    };
    void tick();
    const wake = () => { if (document.visibilityState === "visible") { if (timer) clearTimeout(timer); void tick(); } };
    document.addEventListener("visibilitychange", wake);
    return () => {
      live = false;
      if (timer) clearTimeout(timer);
      document.removeEventListener("visibilitychange", wake);
    };
  }, [ids, nudge]);

  const refresh = React.useCallback(() => setNudge((n) => n + 1), []);
  return { status, loaded, refresh };
}
