'use client';

/*
 * The Boards tab of the Knowledge hub (C7, artboard 4A; BOARD1b): every board a person can open — the shared ones,
 * each workflow's and each schedule's — with the one picked beside the list. The page's frame (OSS `AppScreen`,
 * cloud `OrgScreen`) is the host page's; this is what goes inside it.
 */
import { useCallback, useEffect, useMemo, useState } from 'react';
import { Plus } from 'lucide-react';

import { Button, EmptyState, ErrorNote, Field, Input, Modal, Spinner, Textarea } from '@/components/ui';
import { useHelpTopic } from '@/components/help';
import { cn } from '@/lib/cn';

import { BoardView } from './BoardView';
import { boardsApi, groupBoards, sharedQuota, type BoardScope, type BoardSummary, type BoardsList } from './board';
import { useBoardsHost } from './host';

const GROUP_TITLE: Record<BoardScope, string> = { shared: 'Shared', workflow: 'Workflows', schedule: 'Schedules' };

function isOff(e: unknown): boolean {
    return typeof e === 'object' && e !== null && (e as { status?: number }).status === 404;
}

export function BoardsScreen({ initialBoardId }: { initialBoardId?: string | null }) {
    const host = useBoardsHost();
    const api = useMemo(() => boardsApi(host.request), [host.request]);
    const [list, setList] = useState<BoardsList | null>(null);
    const [off, setOff] = useState(false);
    const [error, setError] = useState('');
    const [selected, setSelected] = useState<string | null>(initialBoardId ?? null);
    const [creating, setCreating] = useState(false);
    const [name, setName] = useState('');
    const [description, setDescription] = useState('');
    const [busy, setBusy] = useState(false);
    const [createError, setCreateError] = useState('');
    useHelpTopic(selected ? 'boards.board' : null);

    const load = useCallback(async () => {
        try {
            const next = await api.list();
            setList(next);
            setOff(false);
            setError('');
            setSelected(s => (s && next.boards.some(b => b.id === s)) ? s : (next.boards[0]?.id ?? null));
        } catch (e) {
            if (isOff(e)) setOff(true);
            else setError(e instanceof Error ? e.message : String(e));
            setList(l => l ?? { boards: [], shared_used: 0, shared_limit: null });
        }
    }, [api]);
    useEffect(() => { load(); }, [load]);

    if (off) {
        return (
            <EmptyState title="Boards are off in this workspace">
                {host.offHint ?? 'Boards are not switched on here yet.'}
            </EmptyState>
        );
    }
    if (!list) return <div className="flex justify-center py-12"><Spinner /></div>;

    const groups = groupBoards(list.boards);
    const quota = sharedQuota(list);
    const atCap = !!list.shared_limit && list.shared_used >= list.shared_limit;

    const create = async () => {
        setBusy(true);
        try {
            const made = await api.create(name.trim(), description.trim());
            setCreating(false);
            setName(''); setDescription(''); setCreateError('');
            await load();
            setSelected(made.id);
        } catch (e) {
            setCreateError(e instanceof Error ? e.message : String(e));
        } finally {
            setBusy(false);
        }
    };

    return (
        <div className="lg:grid lg:grid-cols-[260px_minmax(0,1fr)] lg:items-start lg:gap-8">
            <nav aria-label="Boards" className="mb-6 space-y-5 lg:mb-0">
                {error && <ErrorNote>{error}</ErrorNote>}
                {(['shared', 'workflow', 'schedule'] as BoardScope[]).map(scope => (
                    <section key={scope} className="space-y-1" aria-labelledby={`boards-${scope}`}>
                        <h3 id={`boards-${scope}`} className="px-2.5 text-2xs font-semibold uppercase tracking-wider text-text-faint">{GROUP_TITLE[scope]}</h3>
                        {groups[scope].length === 0 && scope !== 'shared' && (
                            <p className="px-2.5 text-xs text-text-faint">
                                {scope === 'workflow' ? "None yet: a workflow's board starts at its first card." : "None yet: a scheduled agent's board starts at its first card."}
                            </p>
                        )}
                        <ul className="space-y-0.5">
                            {groups[scope].map(b => <BoardLink key={b.id} board={b} current={b.id === selected} onPick={() => setSelected(b.id)} />)}
                        </ul>
                        {scope === 'shared' && (
                            <div className="space-y-1 px-2.5 pt-1">
                                {host.canEdit && (
                                    <Button size="sm" variant="ghost" className="-ml-2.5" disabled={atCap} onClick={() => setCreating(true)}>
                                        <Plus className="size-3.5" aria-hidden /> New shared board
                                    </Button>
                                )}
                                {quota && <p className="text-xs text-text-faint">{quota}{atCap ? ': delete one to make another.' : ''}</p>}
                            </div>
                        )}
                    </section>
                ))}
            </nav>
            <div className="min-w-0">
                {selected ? (
                    <BoardView key={selected} boardRef={{ id: selected }} onChanged={load} onDeleted={() => { setSelected(null); load(); }} />
                ) : (
                    <EmptyState title="No boards yet" action={host.canEdit && !atCap && <Button onClick={() => setCreating(true)}>New shared board</Button>}>
                        A board is shared work agents read, claim and close. A workflow or a scheduled agent gets its own at
                        its first card; a shared board is for work between them, used by what its list names.
                    </EmptyState>
                )}
            </div>
            <Modal open={creating} onClose={() => setCreating(false)} title="New shared board" size="sm"
                description="Agents, workflows and schedules use it only once you name them on its list." footer={
                    <>
                        <Button variant="ghost" onClick={() => setCreating(false)}>Cancel</Button>
                        <Button disabled={busy || !name.trim()} onClick={create}>Create</Button>
                    </>
                }>
                <div className="space-y-3">
                    <Field label="Name"><Input autoFocus value={name} onChange={e => setName(e.target.value)} placeholder="Bug intake" /></Field>
                    <Field label="Description (optional)"><Textarea rows={3} value={description} onChange={e => setDescription(e.target.value)}
                        placeholder="Bugs from the support inbox, one card each." /></Field>
                    {createError && <ErrorNote>{createError}</ErrorNote>}
                </div>
            </Modal>
        </div>
    );
}

function BoardLink({ board, current, onPick }: { board: BoardSummary; current: boolean; onPick: () => void }) {
    const live = board.open + board.claimed;
    return (
        <li>
            <button type="button" onClick={onPick} aria-current={current ? 'page' : undefined}
                className={cn(
                    'flex h-9 w-full items-center gap-2 rounded-md px-2.5 text-left text-sm',
                    'hover:bg-surface-2 focus-visible:outline-2 focus-visible:outline-accent',
                    current ? 'bg-surface-2 font-medium text-text' : 'text-text-muted',
                )}>
                <span className="min-w-0 flex-1 truncate">{board.name}</span>
                <span className="shrink-0 font-mono text-2xs tabular-nums text-text-faint">{live}</span>
            </button>
        </li>
    );
}
