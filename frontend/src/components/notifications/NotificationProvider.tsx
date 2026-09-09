'use client';
/* eslint-disable @typescript-eslint/no-explicit-any */
import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react';
import { useRouter } from 'next/navigation';
import { CheckCircle2, AlertCircle, PauseCircle, X } from 'lucide-react';
import { readWithStallTimeout } from '@/lib/sse';

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
};

type Toast = { key: number; message: string; type: 'success' | 'warning' | 'error' };

type NotificationContextValue = {
    notifications: RunNotification[];
    unseenCount: number;
    markAllSeen: () => void;
    toast: (message: string, type?: Toast['type']) => void;
    browserNotificationsEnabled: boolean;
    setBrowserNotificationsEnabled: (enabled: boolean) => Promise<void>;
    openRun: (runId: string | null) => void;
};

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
    const [notifications, setNotifications] = useState<RunNotification[]>([]);
    const [seenId, setSeenId] = useState(0);
    const [toasts, setToasts] = useState<Toast[]>([]);
    const [browserEnabled, setBrowserEnabled] = useState(false);
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
        if (runId) router.push(`/orchestrations?run=${encodeURIComponent(runId)}`);
        else router.push('/orchestrations');
    }, [router]);

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
        toast(item.title, item.kind === 'run_failed' ? 'error'
            : item.kind === 'human_input' ? 'warning' : 'success');
        if (browserEnabledRef.current && document.hidden
            && typeof Notification !== 'undefined' && Notification.permission === 'granted') {
            try {
                const notification = new Notification(item.title, {
                    body: item.body || undefined,
                    tag: `synapse-${item.id}`,
                });
                notification.onclick = () => {
                    window.focus();
                    openRun(item.run_id);
                    notification.close();
                };
            } catch { /* ignore */ }
        }
    }, [toast, openRun]);

    // Notification SSE: replay-then-tail with reconnect from the last id.
    useEffect(() => {
        let stopped = false;
        const controller = { current: null as AbortController | null };

        const connect = async () => {
            let liveAfterReplay = false;
            while (!stopped) {
                const abort = new AbortController();
                controller.current = abort;
                try {
                    // Always replay the full ring: re-received ids are upserted, and
                    // updates to old items (resolved flags) survive reconnects.
                    const res = await fetch('/api/notifications/stream?after=0',
                        { signal: abort.signal });
                    if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`);
                    const reader = res.body.getReader();
                    const decoder = new TextDecoder();
                    let buffer = '';
                    // Everything already in the ring at connect time is history.
                    const replayCutoff = Date.now() / 1000 - 5;
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
                                    handleNotification(item, liveAfterReplay || item.ts > replayCutoff);
                                } catch { /* ignore */ }
                            }
                        }
                        liveAfterReplay = true;
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
            setBrowserNotificationsEnabled, openRun,
        }}>
            {children}
            {/* Toast host — app-wide, stacked bottom-right, clearing the AI
                builder's launcher below it. Two things cannot own that corner,
                and the launcher is the one the user goes looking for. */}
            <div className="fixed bottom-20 right-6 z-[9999] flex flex-col gap-2 items-end pointer-events-none">
                {toasts.map(t => (
                    <div
                        key={t.key}
                        className={`pointer-events-auto flex items-center gap-2 px-4 py-2.5 rounded-lg border shadow-xl text-sm max-w-sm animate-in ${
                            t.type === 'error' ? 'bg-red-950/95 border-red-800 text-red-200' :
                            t.type === 'warning' ? 'bg-amber-950/95 border-amber-800 text-amber-200' :
                            'bg-zinc-900/95 border-zinc-700 text-zinc-200'
                        }`}
                    >
                        {t.type === 'error' ? <AlertCircle size={15} className="shrink-0" /> :
                         t.type === 'warning' ? <PauseCircle size={15} className="shrink-0" /> :
                         <CheckCircle2 size={15} className="shrink-0 text-green-400" />}
                        <span className="flex-1 min-w-0 truncate">{t.message}</span>
                        <button
                            onClick={() => setToasts(prev => prev.filter(x => x.key !== t.key))}
                            className="shrink-0 opacity-60 hover:opacity-100"
                        >
                            <X size={13} />
                        </button>
                    </div>
                ))}
            </div>
        </NotificationContext.Provider>
    );
}
