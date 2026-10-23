/*
 * Boards for people: the shapes the routes serve, the calls, and the words a card is described in (BOARD1b, D282).
 *
 * Synced to the cloud app byte for byte (`scripts/sync-ui.sh`), so it calls nothing directly: every request goes
 * through the host's `request` (`./host.tsx`), which knows the product's address (`/api/boards…` here, `/bff/boards…`
 * in cloud). Both products' routes serve `core.boards.service`'s shapes. Pure functions only below the calls, so
 * cloud's Vitest can hold them.
 */

/** Settings turned Boards on or off: the shell's tabs and the agent editor read the switch again. */
export const BOARDS_SWITCHED_EVENT = 'synapse-boards-switched';

export type BoardScope = 'shared' | 'workflow' | 'schedule';
export type PrincipalKind = 'agent' | 'workflow' | 'schedule';
export type Access = 'add' | 'work';

export type Actor = { agent?: string; agent_name?: string; run?: string; session?: string; person?: string; source?: string };

export type BoardSummary = {
    id: string | null;
    scope: BoardScope;
    scope_id: string;
    /** A shared board's name; a workflow's or a schedule's name for its own board, said as deleted when its
     *  workflow or schedule is gone ("Invoice chaser (deleted workflow)", never an id: F872). */
    name: string;
    description: string;
    created_by: Actor;
    created_at: string | null;
    /** A workflow's or a schedule's board whose workflow or schedule was deleted. */
    owner_gone: boolean;
    open: number;
    claimed: number;
};

export type Card = {
    id: string;
    board_id: string;
    kind: 'task' | 'note';
    title: string;
    body: string;
    status: 'open' | 'claimed' | 'done' | 'archived';
    outcome: 'succeeded' | 'failed' | null;
    result: string;
    key: string | null;
    version: number;
    holder_agent: string | null;
    holder_key: string | null;
    holder_label: string | null;
    claim_expires_at: string | null;
    created_by: Actor;
    by_person: boolean;
    run_id: string | null;
    tainted: boolean;
    created_at: string | null;
    updated_at: string | null;
    closed_at: string | null;
};

export type CardEvent = {
    card_id: string;
    seq: number;
    kind: 'created' | 'edited' | 'claimed' | 'released' | 'closed' | 'reopened' | 'comment' | 'undo' | 'archived';
    by: Actor;
    before: Record<string, unknown> | null;
    after: Record<string, unknown> | null;
    comment: string;
    tainted: boolean;
    created_at: string | null;
    /** A person can take this change back now. */
    undoable?: boolean;
};

export type AccessEntry = { kind: PrincipalKind; id: string; access: Access; name: string; gone: boolean };

export type CurrentRun = { run_id: string; status?: string | null; started_at?: string | null };

export type BoardPage = {
    board: BoardSummary;
    current_run: CurrentRun | null;
    /** Open and claimed, in reading order: this run's first, then a person's, then tasks oldest first. */
    cards: Card[];
    /** The latest closed, newest first. */
    done: Card[];
    /** On a search, archived cards that match. */
    archived: Card[];
    access: AccessEntry[];
};

export type BoardsList = { boards: BoardSummary[]; shared_used: number; shared_limit: number | null };

export type CardPage = { card: Card; comments: CardEvent[]; history: CardEvent[] };

/** What a board reaches an agent through, when not its own name: a schedule or a workflow that runs it (F583). */
export type Through = { kind: 'workflow' | 'schedule'; id: string; name: string };

export type BoardFor = BoardSummary & { access: Access; in_use: boolean; through?: Through[] };

/** "Through the schedule 'Daily check' and the workflow 'Fix a bug'"; empty for a board that names it itself. */
export function throughLine(through: Through[] | undefined): string {
    const parts = (through ?? []).map(t => `the ${t.kind} ‘${t.name}’`);
    if (!parts.length) return '';
    const joined = parts.length === 1 ? parts[0] : `${parts.slice(0, -1).join(', ')} and ${parts[parts.length - 1]}`;
    return `Through ${joined}`;
}

/** A board's place: one of the list's, or a workflow's or a schedule's own (made at its first card). */
export type BoardRef = { id: string } | { scope: 'workflow' | 'schedule'; scopeId: string };

/** The host's one way to the routes: a path under the boards root, a method and a JSON body. Rejects with the
 *  route's own sentence. */
export type BoardsRequest = <T>(method: 'GET' | 'POST' | 'PATCH' | 'PUT' | 'DELETE', path: string, body?: unknown) => Promise<T>;

export type CardDraft = { title: string; body?: string; kind?: 'task' | 'note'; key?: string | null };
export type CardChange = { version: number; title?: string; body?: string; status?: 'open' | 'done' | 'failed'; result?: string };

function query(params: Record<string, string | undefined>): string {
    const parts = Object.entries(params).filter(([, v]) => v).map(([k, v]) => `${k}=${encodeURIComponent(v as string)}`);
    return parts.length ? `?${parts.join('&')}` : '';
}

const enc = encodeURIComponent;

export function boardsApi(request: BoardsRequest) {
    return {
        list: () => request<BoardsList>('GET', ''),
        create: (name: string, description = '') => request<BoardSummary>('POST', '', { name, description }),
        rename: (id: string, change: { name?: string; description?: string }) =>
            request<BoardSummary>('PATCH', `/${enc(id)}`, change),
        remove: (id: string) => request<void>('DELETE', `/${enc(id)}`),
        setAccess: (id: string, entries: { kind: PrincipalKind; id: string; access: Access }[]) =>
            request<{ access: AccessEntry[] }>('PUT', `/${enc(id)}/access`, { entries }),
        open: (ref: BoardRef, opts: { run?: 'all' | 'this'; q?: string } = {}) => {
            const qs = query({ run: opts.run === 'this' ? 'this' : undefined, q: opts.q?.trim() || undefined });
            return 'id' in ref
                ? request<BoardPage>('GET', `/${enc(ref.id)}${qs}`)
                : request<BoardPage>('GET', `/scoped/${ref.scope}/${enc(ref.scopeId)}${qs}`);
        },
        addCard: (ref: BoardRef, draft: CardDraft) =>
            'id' in ref
                ? request<{ card: Card; added: boolean }>('POST', `/${enc(ref.id)}/cards`, draft)
                : request<{ card: Card; added: boolean }>('POST', `/scoped/${ref.scope}/${enc(ref.scopeId)}/cards`, draft),
        card: (id: string) => request<CardPage>('GET', `/cards/${enc(id)}`),
        change: (id: string, change: CardChange) => request<Card>('PATCH', `/cards/${enc(id)}`, change),
        comment: (id: string, text: string) => request<CardEvent>('POST', `/cards/${enc(id)}/comments`, { text }),
        undo: (id: string, seq: number) => request<Card>('POST', `/cards/${enc(id)}/undo`, { seq }),
        boardsFor: (kind: PrincipalKind, id: string) =>
            request<{ boards: BoardFor[] }>('GET', `/for/${kind}/${enc(id)}`).then(r => r.boards),
    };
}

export type BoardsApi = ReturnType<typeof boardsApi>;

// ── words ──────────────────────────────────────────────────────────────────────

/** The boards list in its three groups, each in the order the routes gave. */
export function groupBoards(boards: BoardSummary[]): Record<BoardScope, BoardSummary[]> {
    const out: Record<BoardScope, BoardSummary[]> = { shared: [], workflow: [], schedule: [] };
    for (const b of boards) out[b.scope]?.push(b);
    return out;
}

/** This run's cards and the earlier ones, each kept in reading order. With no current run every card is earlier. */
export function splitByRun(cards: Card[], runId: string | null | undefined): { thisRun: Card[]; earlier: Card[] } {
    const thisRun: Card[] = [];
    const earlier: Card[] = [];
    for (const c of cards) (runId && c.run_id === runId ? thisRun : earlier).push(c);
    return { thisRun, earlier };
}

/** Open (and claims that lapsed, which read as open) and claimed, apart. */
export function splitByStatus(cards: Card[]): { open: Card[]; claimed: Card[] } {
    return { open: cards.filter(c => c.status === 'open'), claimed: cards.filter(c => c.status === 'claimed') };
}

/** Who wrote a card or made a change, as one name: a person's name, else the agent's. */
export function actorName(by: Actor | null | undefined): string {
    if (!by) return 'Someone';
    return by.person || by.agent_name || by.agent || 'Someone';
}

const DAY = new Intl.DateTimeFormat('en-GB', { day: 'numeric', month: 'short' });
const DAY_TIME = new Intl.DateTimeFormat('en-GB', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', hour12: false });

export function shortDay(iso: string | null | undefined): string {
    if (!iso) return '';
    const ms = Date.parse(iso);
    return Number.isFinite(ms) ? DAY.format(new Date(ms)) : '';
}

export function dayAndTime(iso: string | null | undefined): string {
    if (!iso) return '';
    const ms = Date.parse(iso);
    return Number.isFinite(ms) ? DAY_TIME.format(new Date(ms)).replace(',', '') : '';
}

/**
 * The line under a card's title: who wrote it and when or in which run — "From Priya Raman · 9 Oct" for a
 * person's (an instruction), "Order desk · this run", "Order desk · run 7 Oct 09:00", or the date alone.
 * `runs` names an earlier run by when it started, where the page knows it.
 */
export function provenance(card: Card, currentRunId?: string | null, runs?: Record<string, string>): string {
    const who = actorName(card.created_by);
    if (card.by_person) return `From ${who} · ${shortDay(card.created_at)}`;
    if (card.run_id && currentRunId && card.run_id === currentRunId) return `${who} · this run`;
    if (card.run_id) {
        const started = runs?.[card.run_id];
        return `${who} · run ${started ?? shortDay(card.created_at)}`;
    }
    return `${who} · ${shortDay(card.created_at)}`;
}

/** "held for 4 more minutes" while a claim is in force; '' otherwise. */
export function heldFor(card: Card, now: number): string {
    if (card.status !== 'claimed' || !card.claim_expires_at) return '';
    const left = Date.parse(card.claim_expires_at) - now;
    if (!Number.isFinite(left) || left <= 0) return '';
    const minutes = Math.max(1, Math.round(left / 60_000));
    return `held for ${minutes} more ${minutes === 1 ? 'minute' : 'minutes'}`;
}

/** Who holds a claimed card: the holder's label without its run detail ("Invoice clerk"). */
export function holderName(card: Card): string {
    const label = card.holder_label || card.holder_agent || 'an agent';
    return label.replace(/\s*\(.*\)\s*$/, '');
}

/** A card's state in a word or two. */
export function statusLabel(card: Card): string {
    if (card.status === 'archived') return 'Archived';
    if (card.status === 'done') return card.kind === 'note' ? 'Closed' : card.outcome === 'failed' ? 'Failed' : 'Done';
    if (card.status === 'claimed') return 'Claimed';
    return 'Open';
}

const FIELD_WORDS: Record<string, string> = { title: 'the title', body: 'the text', result: 'the result', status: 'the status' };

/** One line for a change in a card's history: "Order desk edited the title", "Priya Raman took back change 3". */
export function eventLine(event: CardEvent): string {
    const who = actorName(event.by);
    switch (event.kind) {
        case 'created': return `${who} added it`;
        case 'edited': {
            const after = event.after ?? {};
            if ('undoes' in after) return `${who} took back change ${String(after.undoes)}`;
            const fields = Object.keys(after).filter(k => k in FIELD_WORDS).map(k => FIELD_WORDS[k]);
            return fields.length ? `${who} edited ${fields.join(' and ')}` : `${who} edited it`;
        }
        case 'claimed': return `${who} claimed it`;
        case 'released': return `${who}'s claim ended`;
        case 'closed': {
            const after = event.after ?? {};
            if ('undoes' in after) return `${who} took back change ${String(after.undoes)}`;
            return after.outcome === 'failed' ? `${who} closed it as failed` : `${who} closed it`;
        }
        case 'reopened': {
            const after = event.after ?? {};
            return 'undoes' in after ? `${who} took back change ${String(after.undoes)}` : `${who} reopened it`;
        }
        case 'comment': return `${who} commented`;
        case 'undo': return `${who} took back change ${String((event.after ?? {}).undoes ?? '')}`;
        case 'archived': return event.by?.person ? `${who} archived it` : 'Archived after 90 days closed';
        default: return `${who} changed it`;
    }
}

/** Why a card is marked "From content": shown beside the mark (C4, BOARD1a's note made visible). */
export const FROM_CONTENT_HINT =
    "Written after an agent read outside content (a web page, an email, a tool's result). Agents that read it are " +
    'treated the same way: a "remember" asks first.';

/** What a person's card is, said where one is added. */
export const PERSON_CARD_HINT = 'A card you add is an instruction the next run reads first.';

/** The access words, as a person picks them. */
export const ACCESS_LABEL: Record<Access, string> = { add: 'Add cards', work: 'Read, claim and update' };

export const KIND_NOUN: Record<PrincipalKind, string> = { agent: 'Agent', workflow: 'Workflow', schedule: 'Schedule' };

/** The shared-board count against the plan's cap, or null when there is no cap. */
export function sharedQuota(list: Pick<BoardsList, 'shared_used' | 'shared_limit'>): string | null {
    if (!list.shared_limit) return null;
    return `${list.shared_used} of ${list.shared_limit} on your plan`;
}

// ── the board's columns, and what dragging a sticky does (Naveen at BOARD1b's screens) ──────────────────────────

export type Column = 'notes' | 'open' | 'claimed' | 'done';

export const COLUMN_TITLE: Record<Column, string> = { notes: 'Notes', open: 'Open', claimed: 'Claimed', done: 'Done' };

/** A board's stickies by column: notes (things to know), open and claimed tasks, then what is closed (archived
 *  ones too on a search). Each keeps the order the routes gave. */
export function columnsOf(page: Pick<BoardPage, 'cards' | 'done' | 'archived'>): Record<Column, Card[]> {
    return {
        notes: page.cards.filter(c => c.kind === 'note'),
        open: page.cards.filter(c => c.kind === 'task' && c.status === 'open'),
        claimed: page.cards.filter(c => c.kind === 'task' && c.status === 'claimed'),
        done: [...page.done, ...page.archived],
    };
}

/**
 * What dropping `card` on `column` does, or null when it may not go there. A task dropped on Done is finished (the
 * board asks for its result, or why it failed); a note dropped on Done is closed as no longer true; a closed task
 * goes back to Open and a closed note back to Notes. Claimed is the agents' column: a person never drops there.
 * Archived cards come back only through their history's Undo.
 */
export function dropAction(card: Card, column: Column): 'finish' | 'close' | 'reopen' | null {
    const live = card.status === 'open' || card.status === 'claimed';
    if (column === 'done') return live ? (card.kind === 'task' ? 'finish' : 'close') : null;
    if (card.status !== 'done') return null;
    if (column === 'open' && card.kind === 'task') return 'reopen';
    if (column === 'notes' && card.kind === 'note') return 'reopen';
    return null;
}

/** "4 min left" while a claim is in force; '' otherwise (the sticky's short form of `heldFor`). */
export function timeLeft(card: Card, now: number): string {
    if (card.status !== 'claimed' || !card.claim_expires_at) return '';
    const left = Date.parse(card.claim_expires_at) - now;
    if (!Number.isFinite(left) || left <= 0) return '';
    return `${Math.max(1, Math.round(left / 60_000))} min left`;
}
