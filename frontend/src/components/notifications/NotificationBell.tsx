'use client';
import { useState } from 'react';
import Link from 'next/link';
import * as RPopover from '@radix-ui/react-popover';
import { Bell, CheckCircle2, AlertCircle, PauseCircle, ShieldAlert } from 'lucide-react';
import { useNotifications, type RunNotification } from './NotificationProvider';
import { answerToolApproval } from './ToolApproval';
import { plainText } from '@/lib/plainText';
import { voiceItemHref } from '@/components/phone/phone';
import { Button, Checkbox } from '@/components/ui';

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
function ApprovalItem({ n, onOpen }: { n: RunNotification; onOpen: () => void }) {
    // A phone call's question (V16): its call opens from here, to read what the caller asked first.
    const call = voiceItemHref(n.data);
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState('');
    const args = n.data?.tool_args;
    const argText = args && Object.keys(args).length > 0 ? JSON.stringify(args) : '';

    const answer = async (approve: boolean) => {
        setBusy(true);
        setError('');
        try {
            // Success needs nothing here: the server re-sends this item resolved.
            await answerToolApproval(n, approve);
        } catch (e) {
            setError(e instanceof Error ? e.message : 'That answer did not go through.');
        } finally {
            setBusy(false);
        }
    };

    return (
        <div className="border-b border-border px-3 py-2.5">
            <div className="flex items-start gap-2">
                <ShieldAlert size={14} className="mt-0.5 shrink-0 text-person-ink" aria-hidden />
                <span className="min-w-0 flex-1">
                    <span className="block text-[13px] text-text">{n.title}</span>
                    {n.body && <span className="block text-xs text-text-muted">{plainText(n.body)}</span>}
                    {argText && (
                        <code className="mt-1 block truncate font-code text-xs text-text-muted" title={argText}>{argText}</code>
                    )}
                </span>
            </div>
            <div className="mt-2 flex items-center gap-2 pl-6">
                <Button size="sm" disabled={busy} onClick={() => answer(true)}>Approve</Button>
                <Button size="sm" variant="secondary" disabled={busy} onClick={() => answer(false)}>Refuse</Button>
                {call && (
                    <Link href={call} onClick={onOpen} className="whitespace-nowrap text-xs text-accent-ink underline-offset-2 hover:underline">
                        Open the call
                    </Link>
                )}
                <span className="text-xs text-text-faint">
                    {new Date(n.ts * 1000).toLocaleTimeString()}
                </span>
            </div>
            {error && <p role="alert" className="m-0 mt-1 pl-6 text-xs text-danger">{error}</p>}
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
export function NotificationBell({
    side = 'bottom', align = 'start',
}: {
    /** Where the panel opens: below a bell in a top bar, to the right of one at the rail's foot. */
    side?: 'bottom' | 'right';
    align?: 'start' | 'end';
} = {}) {
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
                className="relative rounded-md p-2 text-text-muted transition-colors hover:bg-row-hover hover:text-text data-[state=open]:bg-surface-2 data-[state=open]:text-text"
            >
                <Bell className="h-4 w-4" aria-hidden />
                {unseenCount > 0 && (
                    // The accent, not red: red is for a failure only (UX1), and this counts everything new.
                    <span aria-hidden className="absolute -right-0.5 -top-0.5 flex h-4 min-w-[16px] items-center justify-center rounded-full bg-accent px-1 text-[10px] font-semibold text-accent-fg">
                        {unseenCount > 9 ? '9+' : unseenCount}
                    </span>
                )}
            </RPopover.Trigger>

            <RPopover.Portal>
                <RPopover.Content
                    side={side}
                    align={align}
                    sideOffset={4}
                    collisionPadding={8}
                    aria-label="Notifications"
                    className="z-50 w-80 max-w-[calc(100vw-16px)] overflow-hidden rounded-lg border border-border-strong bg-surface shadow-elevated"
                >
                    <div className="border-b border-border px-3 py-2.5 text-[13px] font-semibold text-text">
                        Notifications
                    </div>
                    <div className="max-h-80 overflow-y-auto">
                        {recent.length === 0 ? (
                            <div className="px-3 py-6 text-center text-[13px] text-text-faint">
                                No notifications yet.
                            </div>
                        ) : recent.map(n => {
                            if (n.kind === 'tool_approval' && !n.resolved) return <ApprovalItem key={n.id} n={n} onOpen={() => setOpen(false)} />;
                            const answered = (n.kind === 'human_input' || n.kind === 'tool_approval') && n.resolved;
                            return (
                            <button
                                key={n.id}
                                onClick={() => { setOpen(false); openItem(n); }}
                                className={`flex w-full items-start gap-2 border-b border-border px-3 py-2.5 text-left transition-colors hover:bg-row-hover ${answered ? 'opacity-60' : ''}`}
                            >
                                {/* Quiet success; red only for a failure; a person's wait is the warm ink (UX1). */}
                                {answered ? <CheckCircle2 size={14} className="mt-0.5 shrink-0 text-text-faint" aria-hidden /> :
                                 n.kind === 'human_input' ? <PauseCircle size={14} className="mt-0.5 shrink-0 text-person-ink" aria-hidden /> :
                                 n.kind === 'run_failed' ? <AlertCircle size={14} className="mt-0.5 shrink-0 text-danger" aria-hidden /> :
                                 <CheckCircle2 size={14} className="mt-0.5 shrink-0 text-text-faint" aria-hidden />}
                                <span className="min-w-0 flex-1">
                                    <span className="block truncate text-[13px] text-text">{n.title}</span>
                                    {n.body && <span className="block truncate text-xs text-text-muted">{plainText(n.body)}</span>}
                                    <span className="mt-0.5 block text-xs text-text-faint">
                                        {new Date(n.ts * 1000).toLocaleTimeString()}
                                        {answered ? ` · ${RESOLVED_AS[n.resolved_as ?? 'answered'] ?? n.resolved_as}` : ''}
                                    </span>
                                </span>
                            </button>
                            );
                        })}
                    </div>
                    <div className="flex items-center gap-2 border-t border-border px-3 py-2.5">
                        <Checkbox
                            id="bell-browser-notifications"
                            label="Browser notifications when tab is in background"
                            checked={browserNotificationsEnabled}
                            onChange={setBrowserNotificationsEnabled}
                        />
                        <label htmlFor="bell-browser-notifications" className="cursor-pointer text-xs text-text-muted">
                            Browser notifications when tab is in background
                        </label>
                    </div>
                </RPopover.Content>
            </RPopover.Portal>
        </RPopover.Root>
    );
}
