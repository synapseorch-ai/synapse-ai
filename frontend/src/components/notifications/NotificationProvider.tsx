'use client';
/* eslint-disable @typescript-eslint/no-explicit-any */
import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react';
import { useRouter } from 'next/navigation';
import { LogIn, PauseCircle, X } from 'lucide-react';
import { readWithStallTimeout } from '@/lib/sse';
import { plainText } from '@/lib/plainText';
import { voiceItemHref } from '@/components/phone/phone';
import { toastLook, useIsCanvasMode } from '@/components/ui';

export type RunNotification = {
    id: number;
    ts: number;
    kind: 'human_input' | 'run_completed' | 'run_failed' | string;
    title: string;
    body: string;
    run_id: string | null;
    data: Record<string, any>;
    // Set server-side once the requested input was submitted (or the run ended).
    resolved?: boolean;
    /** How a human_input item was resolved: answered, or its run cancelled (F193);
     *  a tool_approval item: allowed, refused, or expired unrun (F268). */
    resolved_as?: 'answered' | 'cancelled' | 'allowed' | 'refused' | 'expired';
};

type Toast = { key: number; message: string; type: 'success' | 'warning' | 'error' | 'person' };

type NotificationContextValue = {
    notifications: RunNotification[];
    unseenCount: number;
    markAllSeen: () => void;
    toast: (message: string, type?: Toast['type']) => void;
    browserNotificationsEnabled: boolean;
    setBrowserNotificationsEnabled: (enabled: boolean) => Promise<void>;
    openRun: (runId: string | null) => void;
    /** Where an item leads: its run, or for a schedule's item the Schedules screen;
     *  an unanswered question about a run opens the Waiting on you drawer (F457). */
    openItem: (item: RunNotification) => void;
    /** The one Waiting on you drawer (F457): open or not, and the run it was opened for,
     *  whose card it shows first. The rail's and the phone bar's rows open it, and so
     *  does an unanswered question in the bell (or an OS notification for one). */
    waiting: { open: boolean; focus: string | null };
    openWaiting: (focus?: string | null) => void;
    closeWaiting: () => void;
};

/** An unanswered question about a run, answered in the drawer on the screen the person is
 *  on, not by leaving it (F457, Naveen). An answered one, or a call's item, keeps its link. */
export function opensWaitingDrawer(item: Pick<RunNotification, 'kind' | 'run_id' | 'resolved' | 'data'>): boolean {
    return item.kind === 'human_input' && !item.resolved && !!item.run_id && !voiceItemHref(item.data);
}

const NotificationContext = createContext<NotificationContextValue | null>(null);

export function useNotifications(): NotificationContextValue {
    const ctx = useContext(NotificationContext);
    if (!ctx) throw new Error('useNotifications must be used inside NotificationProvider');
    return ctx;
}

const SEEN_KEY = 'synapseNotifSeenId';
const BROWSER_KEY = 'synapseBrowserNotifs';

// Announced so other components (e.g. ActiveRunsBanner) can react instantly
// to run state changes without their own polling cadence.
export const NOTIFICATION_EVENT = 'synapse-notification';

export function NotificationProvider({ children }: { children: React.ReactNode }) {
    const router = useRouter();
    const canvas = useIsCanvasMode();
    const [notifications, setNotifications] = useState<RunNotification[]>([]);
    const [seenId, setSeenId] = useState(0);
    const [toasts, setToasts] = useState<Toast[]>([]);
    const [browserEnabled, setBrowserEnabled] = useState(false);
    // The session ended while this tab was open (expired, signed out elsewhere,
    // the access link rotated). Said once and kept on screen, rather than
    // retried every 5 s or sent to /login under the person's unsaved work.
    const [signedOut, setSignedOut] = useState(false);
    const browserEnabledRef = useRef(false);
    const lastIdRef = useRef(0);
    const toastKeyRef = useRef(0);

    useEffect(() => {
        setSeenId(parseInt(localStorage.getItem(SEEN_KEY) || '0', 10) || 0);
        const enabled = localStorage.getItem(BROWSER_KEY) === '1'
            && typeof Notification !== 'undefined' && Notification.permission === 'granted';
        setBrowserEnabled(enabled);
        browserEnabledRef.current = enabled;
    }, []);

    const toast = useCallback((message: string, type: Toast['type'] = 'success') => {
        const key = ++toastKeyRef.current;
        setToasts(prev => [...prev.slice(-3), { key, message, type }]);
        setTimeout(() => setToasts(prev => prev.filter(t => t.key !== key)), 5000);
    }, []);

    const openRun = useCallback((runId: string | null) => {
        if (runId) router.push(`/workflows?run=${encodeURIComponent(runId)}`);
        else router.push('/workflows');
    }, [router]);

    const [waiting, setWaiting] = useState<{ open: boolean; focus: string | null }>({ open: false, focus: null });
    const openWaiting = useCallback((focus?: string | null) => setWaiting({ open: true, focus: focus ?? null }), []);
    const closeWaiting = useCallback(() => setWaiting(w => ({ ...w, open: false })), []);

    const openItem = useCallback((item: RunNotification) => {
        if (opensWaitingDrawer(item)) {
            openWaiting(item.run_id);
            return;
        }
        // A call's voicemail, hand-off or parked approval has no run: it opens that call's log (VOX1c).
        const call = voiceItemHref(item.data);
        if (call) router.push(call);
        else if (!item.run_id && item.data?.schedule_id) router.push('/schedules');
        else openRun(item.run_id);
    }, [router, openRun, openWaiting]);

    const handleNotification = useCallback((item: RunNotification, live: boolean) => {
        setNotifications(prev => {
            // Upsert: a re-delivered id is an update (e.g. human input resolved).
            if (prev.some(n => n.id === item.id)) {
                return prev.map(n => (n.id === item.id ? item : n));
            }
            return [...prev.slice(-99), item];
        });
        if (!live || item.resolved) return;  // history/updates: no toast/browser popup
        window.dispatchEvent(new CustomEvent(NOTIFICATION_EVENT, { detail: item }));
        // A run that needs someone is the one warm toast (UX1: amber is a person, M2h-2).
        toast(item.title, item.kind === 'run_failed' ? 'error'
            : item.kind === 'human_input' || item.kind === 'tool_approval' ? 'person' : 'success');
        if (browserEnabledRef.current && document.hidden
            && typeof Notification !== 'undefined' && Notification.permission === 'granted') {
            try {
                const notification = new Notification(item.title, {
                    // The OS draws text, not markdown.
                    body: item.body ? plainText(item.body) : undefined,
                    tag: `synapse-${item.id}`,
                });
                notification.onclick = () => {
                    window.focus();
                    openItem(item);
                    notification.close();
                };
            } catch { /* ignore */ }
        }
    }, [toast, openItem]);

    // Notifications: history first, then the live tail, on every connection.
    //
    // What the ring already holds when this page connects is history — listed
    // and badged, never toasted (F216). It used to be told apart by timing: the
    // stream's first read chunk and a 5 s clock window, with the flag kept
    // across reconnects. A replay longer than one chunk, or any reconnect (a
    // backend restart, a dropped relay), toasted old runs again — a failure
    // since resumed among them — and raised them as OS notifications in a
    // hidden tab. Now the boundary is an id, and an id is announced once.
    useEffect(() => {
        let stopped = false;
        const controller = { current: null as AbortController | null };
        const announced = new Set<number>();

        const connect = async () => {
            while (!stopped) {
                const abort = new AbortController();
                controller.current = abort;
                try {
                    // The ring as it is now. Read on each connection, so a
                    // reconnect also picks up what changed meanwhile (a
                    // resolved flag).
                    const listRes = await fetch('/api/notifications?after=0', { signal: abort.signal });
                    // Not signed in: no retry would change that (F178).
                    if (listRes.status === 401) {
                        if (!stopped) setSignedOut(true);
                        return;
                    }
                    if (!listRes.ok) throw new Error(`HTTP ${listRes.status}`);
                    const history = (await listRes.json()) as { notifications?: RunNotification[]; last_id?: number };
                    let boundary = history.last_id ?? 0;
                    for (const item of history.notifications ?? []) {
                        announced.add(item.id);
                        boundary = Math.max(boundary, item.id);
                        handleNotification(item, false);
                    }
                    if (boundary > lastIdRef.current) lastIdRef.current = boundary;
                    // Seen past the newest one there is: the server's notifications began again (lost in a reset
                    // or a new install) while this browser kept the last id it had seen, so every new one read as
                    // seen and the badge stayed dark (F479's class). None of these was seen here.
                    setSeenId(prev => {
                        if (prev <= boundary) return prev;
                        localStorage.setItem(SEEN_KEY, '0');
                        return 0;
                    });

                    // The tail from there. The server subscribes before it
                    // replays, so anything published after the list arrives
                    // here; an id already announced is an update, not news.
                    const res = await fetch(`/api/notifications/stream?after=${boundary}`,
                        { signal: abort.signal });
                    if (res.status === 401) {
                        if (!stopped) setSignedOut(true);
                        return;
                    }
                    if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`);
                    const reader = res.body.getReader();
                    const decoder = new TextDecoder();
                    let buffer = '';
                    while (true) {
                        const { done, value } = await readWithStallTimeout(reader, abort);
                        if (done) break;
                        buffer += decoder.decode(value, { stream: true });
                        const lines = buffer.split('\n');
                        buffer = lines.pop() || '';
                        for (const line of lines) {
                            if (line.startsWith('data: ')) {
                                try {
                                    const item = JSON.parse(line.slice(6)) as RunNotification;
                                    if (item.id > lastIdRef.current) lastIdRef.current = item.id;
                                    const news = !announced.has(item.id);
                                    announced.add(item.id);
                                    handleNotification(item, news);
                                } catch { /* ignore */ }
                            }
                        }
                    }
                } catch { /* fall through to retry */ }
                if (stopped) return;
                await new Promise<void>(r => setTimeout(r, 5000));
            }
        };
        connect();
        return () => { stopped = true; controller.current?.abort(); };
    }, [handleNotification]);

    const unseenCount = notifications.filter(n => n.id > seenId).length;

    const markAllSeen = useCallback(() => {
        const maxId = lastIdRef.current;
        setSeenId(maxId);
        localStorage.setItem(SEEN_KEY, String(maxId));
    }, []);

    const setBrowserNotificationsEnabled = useCallback(async (enabled: boolean) => {
        if (!enabled) {
            localStorage.setItem(BROWSER_KEY, '0');
            setBrowserEnabled(false);
            browserEnabledRef.current = false;
            return;
        }
        if (typeof Notification === 'undefined') return;
        const permission = Notification.permission === 'granted'
            ? 'granted' : await Notification.requestPermission();
        const granted = permission === 'granted';
        localStorage.setItem(BROWSER_KEY, granted ? '1' : '0');
        setBrowserEnabled(granted);
        browserEnabledRef.current = granted;
    }, []);

    return (
        <NotificationContext.Provider value={{
            notifications, unseenCount, markAllSeen, toast,
            browserNotificationsEnabled: browserEnabled,
            setBrowserNotificationsEnabled, openRun, openItem,
            waiting, openWaiting, closeWaiting,
        }}>
            {children}
            {/* Toast host — app-wide, stacked bottom-right, high enough to clear
                a composer's Send at the bottom of chat. On an open
                canvas that corner and the bottom row are the canvas's (zoom, the
                dock, the run strip with its Answer), so toasts come down from the
                top, centred under its bar (M3a: on a phone a toast covered Answer). */}
            <div className={canvas
                ? 'fixed left-1/2 top-[104px] z-[9999] flex -translate-x-1/2 flex-col items-center gap-2 pointer-events-none md:top-[70px]'
                : 'fixed bottom-20 right-6 z-[9999] flex flex-col gap-2 items-end pointer-events-none'}>
                {signedOut && (
                    <div
                        role="status"
                        className={`pointer-events-auto flex items-center gap-2 px-4 py-2.5 rounded-lg border shadow-elevated text-sm max-w-sm ${toastLook('warning').chip}`}
                    >
                        <LogIn size={15} className="shrink-0" aria-hidden />
                        <span className="flex-1 min-w-0">You&apos;re signed out.</span>
                        {/* A full navigation, and only when asked: whatever is
                            unsaved on this page stays until then. */}
                        <button
                            onClick={() => window.location.assign(
                                `/login?redirect=${encodeURIComponent(window.location.pathname + window.location.search)}`)}
                            className="shrink-0 font-semibold underline underline-offset-2 hover:opacity-80"
                        >
                            Sign in
                        </button>
                    </div>
                )}
                {toasts.map(t => {
                    // The kit Toast's tones (components/ui/toast.tsx), on a host of
                    // its own because this one stacks and can be dismissed.
                    const look = toastLook(t.type === 'error' ? 'danger' : t.type);
                    const Icon = t.type === 'person' ? PauseCircle : look.icon;
                    return (
                    <div
                        key={t.key}
                        className={`pointer-events-auto flex items-center gap-2 px-4 py-2.5 rounded-lg border shadow-elevated text-sm max-w-sm animate-in ${look.chip}`}
                    >
                        <Icon size={15} className={`shrink-0 ${look.mark}`} aria-hidden />
                        {/* Wraps, up to three lines, rather than cutting a reason off mid-sentence: a refusal
                            toasted here says why (F518's Run now); the bell's own titles are one line anyway. */}
                        <span className="flex-1 min-w-0 line-clamp-3 break-words">{t.message}</span>
                        <button
                            onClick={() => setToasts(prev => prev.filter(x => x.key !== t.key))}
                            aria-label="Dismiss this notification"
                            className="shrink-0 opacity-60 hover:opacity-100"
                        >
                            <X size={13} />
                        </button>
                    </div>
                    );
                })}
            </div>
        </NotificationContext.Provider>
    );
}
