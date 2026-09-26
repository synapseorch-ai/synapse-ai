'use client';
import { useState } from 'react';
import * as RPopover from '@radix-ui/react-popover';
import { Bell, CheckCircle2, AlertCircle, PauseCircle } from 'lucide-react';
import { useNotifications } from './NotificationProvider';

// Header bell: unseen-count badge, dropdown of recent run notifications
// (click-through to the run view) and the browser-notification opt-in.
//
// On Radix, like the kit's menus and combobox (F192): it used to be a
// hand-placed panel anchored by its right edge under the bell, which from the
// rail's header put 97 px of it off the left of the screen. Radix places it
// against the viewport, portals it out of the rail, closes it on Escape and
// outside clicks, and hands focus back to the bell.
export function NotificationBell() {
    const {
        notifications, unseenCount, markAllSeen, openRun,
        browserNotificationsEnabled, setBrowserNotificationsEnabled,
    } = useNotifications();
    const [open, setOpen] = useState(false);

    const recent = [...notifications].sort((a, b) => b.id - a.id).slice(0, 15);

    return (
        <RPopover.Root
            open={open}
            onOpenChange={next => {
                setOpen(next);
                if (next) markAllSeen();
            }}
        >
            <RPopover.Trigger
                // The name is the control's, with the count beside it — the
                // badge alone used to be the whole name ("6").
                aria-label={unseenCount > 0 ? `Notifications, ${unseenCount} unread` : 'Notifications'}
                title="Notifications"
                className="relative p-2 hover:bg-zinc-900 rounded-md text-zinc-400 hover:text-white transition-colors data-[state=open]:bg-zinc-900 data-[state=open]:text-white"
            >
                <Bell className="h-4 w-4" aria-hidden />
                {unseenCount > 0 && (
                    <span aria-hidden className="absolute -top-0.5 -right-0.5 min-w-[16px] h-4 px-1 rounded-full bg-red-600 text-white text-[10px] font-semibold flex items-center justify-center">
                        {unseenCount > 9 ? '9+' : unseenCount}
                    </span>
                )}
            </RPopover.Trigger>

            <RPopover.Portal>
                <RPopover.Content
                    side="bottom"
                    align="start"
                    sideOffset={4}
                    collisionPadding={8}
                    aria-label="Notifications"
                    className="z-50 w-80 max-w-[calc(100vw-16px)] rounded-lg border border-zinc-700 bg-zinc-900 shadow-2xl overflow-hidden"
                >
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
                                        {answered ? (n.resolved_as === 'cancelled' ? ' · cancelled' : ' · answered') : ''}
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
                </RPopover.Content>
            </RPopover.Portal>
        </RPopover.Root>
    );
}
