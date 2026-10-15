'use client';

/*
 * The agent editor's Boards section (C2, C8; BOARD1b): the switch for the board of the workflow or schedule the
 * agent runs in (`Agent.boards.own`, on unless turned off), and, read-only, the shared boards that reach it: those
 * whose access lists name it, then those that come through a schedule or workflow running it, saying which (F583) —
 * each board's own page edits its list. Shown while Boards are on, folded until opened, as Tool Policy is
 * (Naveen, 2026-10-16: optional, not to scare anyone); the folded row says what is set.
 */
import { useEffect, useMemo, useState } from 'react';

import { Section, Switch } from '@/components/ui';

import { ACCESS_LABEL, boardsApi, throughLine, type BoardFor } from './board';
import { useBoardsHost } from './host';

export function AgentBoards({ agentId, own, onOwn, disabled }: {
    /** The saved agent's id; null for one not saved yet (it is on no list). */
    agentId: string | null;
    own: boolean;
    onOwn: (own: boolean) => void;
    disabled?: boolean;
}) {
    const host = useBoardsHost();
    const api = useMemo(() => boardsApi(host.request), [host.request]);
    const [shared, setShared] = useState<BoardFor[] | null>(null);

    useEffect(() => {
        if (!agentId) return;
        let live = true;
        api.boardsFor('agent', agentId).then(b => { if (live) setShared(b); }).catch(() => { if (live) setShared([]); });
        return () => { live = false; };
    }, [api, agentId]);

    const used = (shared ?? []).filter(b => b.in_use).length;
    const summary = `Own board ${own ? 'on' : 'off'}${used ? ` · ${used} shared` : ''}`;

    return (
        <Section title="Boards" description="The boards it reads and works." summary={summary}>
            <div className="space-y-4">
                <div className="flex items-start justify-between gap-6">
                    <div>
                        <p className="text-sm font-medium text-text">The board of the workflow or schedule it runs in</p>
                        <p className="text-xs leading-relaxed text-text-muted">
                            In a workflow it reads and works that workflow&apos;s board; run by a schedule, that schedule&apos;s. Cards
                            last across runs, and this run&apos;s come first.
                        </p>
                    </div>
                    <Switch checked={own} onChange={onOwn} disabled={disabled} label="The board of the workflow or schedule it runs in" className="mt-0.5" />
                </div>
                <div className="space-y-1.5">
                    <p className="text-sm font-medium text-text">Shared boards</p>
                    {agentId && shared === null ? null : !agentId || !shared?.length ? (
                        <p className="text-xs text-text-muted">
                            None. A shared board&apos;s list names the agents, workflows and schedules that use it: add this agent, or a workflow or schedule that runs it, there.
                        </p>
                    ) : (
                        <ul className="space-y-1">
                            {(shared ?? []).map(b => (
                                <li key={b.id} className="flex min-h-9 items-center gap-2 text-sm">
                                    <span className="min-w-0 flex-1">
                                        {host.boardHref && b.id ? (
                                            <a href={host.boardHref(b.id)} className="block truncate text-text underline-offset-2 hover:underline">{b.name}</a>
                                        ) : <span className="block truncate text-text">{b.name}</span>}
                                        {b.through?.length ? (
                                            <span className="block truncate text-xs text-text-muted">{throughLine(b.through)}</span>
                                        ) : null}
                                    </span>
                                    <span className="shrink-0 text-xs text-text-muted">
                                        {ACCESS_LABEL[b.access]}{b.in_use ? '' : ' · past its 3, not used'}
                                    </span>
                                </li>
                            ))}
                        </ul>
                    )}
                </div>
            </div>
        </Section>
    );
}
