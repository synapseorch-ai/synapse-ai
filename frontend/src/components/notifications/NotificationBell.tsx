'use client';
import { useState } from 'react';
import * as RPopover from '@radix-ui/react-popover';
import { Bell, CheckCircle2, AlertCircle, PauseCircle, ShieldAlert } from 'lucide-react';
import { useNotifications, type RunNotification } from './NotificationProvider';
import { plainText } from '@/lib/plainText';

/** How a resolved item ended, as its row says it. */
const RESOLVED_AS: Record<string, string> = {
    answered: 'answered', cancelled: 'cancelled',
    allowed: 'allowed', refused: 'refused', expired: 'expired — not run',
};

/**
 * A scheduled agent's tool call waiting on a person (F268): the call, and the
 * answer, right here. Nobody is in a chat to see a card, so the bell is where
 * the question is asked; the answer resumes the same schedule run.
 */
function ApprovalItem({ n }: { n: RunNotification }) {
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState('');
    const args = n.data?.tool_args;
    const argText = args && Object.keys(args).length > 0 ? JSON.stringify(args) : '';

    const answer = async (approve: boolean) => {
        setBusy(true);
        setError('');
        try {
            const res = await fetch(`/api/schedules/${encodeURIComponent(n.data?.schedule_id)}/approve`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ approve, approval_id: n.data?.approval_id }),
            });
            if (!res.ok) {
                const detail = (await res.json().catch(() => ({}))).detail;
                setError(typeof detail === 'string' ? detail : 'That answer did not go through.');
            }
            // Success needs nothing here: the server re-sends this item resolved.
        } catch {
            setError('That answer did not go through.');
        } finally {
            setBusy(false);
        }
    };

    return (
        <div className="px-3 py-2 border-b border-zinc-800/50">
            <div className="flex items-start gap-2">
                <ShieldAlert size={14} className="text-amber-400 shrink-0 mt-0.5" aria-hidden />
                <span className="flex-1 min-w-0">
                    <span className="block text-xs text-zinc-200">{n.title}</span>
                    {n.body && <span className="block text-[11px] text-text-faint">{plainText(n.body)}</span>}
                    {argText && (
                        <code className="mt-1 block truncate font-code text-[11px] text-zinc-300" title={argText}>{argText}</code>
                    )}
                </span>
            </div>
            <div className="mt-2 flex items-center gap-2 pl-6">
                <button
                    type="button"
                    disabled={busy}
                    onClick={() => answer(true)}
                    className="px-2.5 py-1 bg-accent text-accent-fg text-[11px] font-semibold rounded-md hover:bg-accent-hover disabled:opacity-50"
                >
                    Approve
                </button>
                <button
                    type="button"
                    disabled={busy}
                    onClick={() => answer(false)}
                    className="px-2.5 py-1 border border-zinc-700 text-zinc-300 text-[11px] font-semibold rounded-md hover:bg-zinc-800 disabled:opacity-50"
                >
                    Refuse
                </button>
                <span className="text-[10px] text-text-faint">
                    {new Date(n.ts * 1000).toLocaleTimeString()}
                </span>
            </div>
            {error && <p role="alert" className="mt-1 pl-6 text-[11px] text-red-400">{error}</p>}
        </div>
    );
}

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
        notifications, unseenCount, markAllSeen, openItem,
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
                            if (n.kind === 'tool_approval' && !n.resolved) return <ApprovalItem key={n.id} n={n} />;
                            const answered = (n.kind === 'human_input' || n.kind === 'tool_approval') && n.resolved;
                            return (
                            <button
                                key={n.id}
                                onClick={() => { setOpen(false); openItem(n); }}
                                className={`w-full flex items-start gap-2 px-3 py-2 text-left hover:bg-zinc-800/70 transition-colors border-b border-zinc-800/50 ${answered ? 'opacity-50' : ''}`}
                            >
                                {answered ? <CheckCircle2 size={14} className="text-text-faint shrink-0 mt-0.5" /> :
                                 n.kind === 'human_input' ? <PauseCircle size={14} className="text-amber-400 shrink-0 mt-0.5" /> :
                                 n.kind === 'run_failed' ? <AlertCircle size={14} className="text-red-400 shrink-0 mt-0.5" /> :
                                 <CheckCircle2 size={14} className="text-green-400 shrink-0 mt-0.5" />}
                                <span className="flex-1 min-w-0">
                                    <span className="block text-xs text-zinc-200 truncate">{n.title}</span>
                                    {n.body && <span className="block text-[11px] text-text-faint truncate">{plainText(n.body)}</span>}
                                    <span className="block text-[10px] text-text-faint mt-0.5">
                                        {new Date(n.ts * 1000).toLocaleTimeString()}
                                        {answered ? ` · ${RESOLVED_AS[n.resolved_as ?? 'answered'] ?? n.resolved_as}` : ''}
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
