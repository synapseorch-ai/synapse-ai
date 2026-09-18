'use client';
import { useEffect, useRef, useState } from 'react';
import { Bell, CheckCircle2, AlertCircle, PauseCircle } from 'lucide-react';
import { useNotifications } from './NotificationProvider';

// Header bell: unseen-count badge, dropdown of recent run notifications
// (click-through to the run view) and the browser-notification opt-in.
export function NotificationBell({ placement = 'below-right' }: {
    /**
     * 'below-right' anchors under the button (chat header). 'above-left' opens
     * upward from the left edge, for the rail foot at the bottom of the screen.
     */
    placement?: 'below-right' | 'above-left';
} = {}) {
    const {
        notifications, unseenCount, markAllSeen, openRun,
        browserNotificationsEnabled, setBrowserNotificationsEnabled,
    } = useNotifications();
    const [open, setOpen] = useState(false);
    const panelRef = useRef<HTMLDivElement>(null);

    useEffect(() => {
        if (!open) return;
        markAllSeen();
        const onClick = (e: MouseEvent) => {
            if (panelRef.current && !panelRef.current.contains(e.target as Node)) setOpen(false);
        };
        document.addEventListener('mousedown', onClick);
        return () => document.removeEventListener('mousedown', onClick);
    }, [open, markAllSeen]);

    const recent = [...notifications].sort((a, b) => b.id - a.id).slice(0, 15);

    return (
        <div className="relative" ref={panelRef}>
            <button
                onClick={() => setOpen(o => !o)}
                className="relative p-2 hover:bg-zinc-900 rounded-md text-zinc-400 hover:text-white transition-colors"
                title="Notifications"
            >
                <Bell className="h-4 w-4" />
                {unseenCount > 0 && (
                    <span className="absolute -top-0.5 -right-0.5 min-w-[16px] h-4 px-1 rounded-full bg-red-600 text-white text-[10px] font-semibold flex items-center justify-center">
                        {unseenCount > 9 ? '9+' : unseenCount}
                    </span>
                )}
            </button>

            {open && (
                <div className={`absolute w-80 rounded-lg border border-zinc-700 bg-zinc-900 shadow-2xl z-[999] overflow-hidden ${placement === 'above-left' ? 'bottom-full left-0 mb-1' : 'right-0 top-full mt-1'}`}>
                    <div className="px-3 py-2 border-b border-zinc-800 text-xs font-semibold text-zinc-300 uppercase tracking-wider">
                        Notifications
                    </div>
                    <div className="max-h-80 overflow-y-auto">
                        {recent.length === 0 ? (
                            <div className="px-3 py-6 text-xs text-text-faint text-center italic">
                                No notifications yet.
                            </div>
                        ) : recent.map(n => {
                            const answered = n.kind === 'human_input' && n.resolved;
                            return (
                            <button
                                key={n.id}
                                onClick={() => { setOpen(false); openRun(n.run_id); }}
                                className={`w-full flex items-start gap-2 px-3 py-2 text-left hover:bg-zinc-800/70 transition-colors border-b border-zinc-800/50 ${answered ? 'opacity-50' : ''}`}
                            >
                                {answered ? <CheckCircle2 size={14} className="text-text-faint shrink-0 mt-0.5" /> :
                                 n.kind === 'human_input' ? <PauseCircle size={14} className="text-amber-400 shrink-0 mt-0.5" /> :
                                 n.kind === 'run_failed' ? <AlertCircle size={14} className="text-red-400 shrink-0 mt-0.5" /> :
                                 <CheckCircle2 size={14} className="text-green-400 shrink-0 mt-0.5" />}
                                <span className="flex-1 min-w-0">
                                    <span className="block text-xs text-zinc-200 truncate">{n.title}</span>
                                    {n.body && <span className="block text-[11px] text-text-faint truncate">{n.body}</span>}
                                    <span className="block text-[10px] text-text-faint mt-0.5">
                                        {new Date(n.ts * 1000).toLocaleTimeString()}
                                        {answered ? ' · answered' : ''}
                                    </span>
                                </span>
                            </button>
                            );
                        })}
                    </div>
                    <label className="flex items-center gap-2 px-3 py-2.5 border-t border-zinc-800 cursor-pointer hover:bg-zinc-800/50">
                        <input
                            type="checkbox"
                            checked={browserNotificationsEnabled}
                            onChange={e => setBrowserNotificationsEnabled(e.target.checked)}
                            className="accent-blue-500"
                        />
                        <span className="text-[11px] text-zinc-400">
                            Browser notifications when tab is in background
                        </span>
                    </label>
                </div>
            )}
        </div>
    );
}
