/*
 * A Team agent's turn, from its events (TEAM1b, D286; 20-agent-teams.md Q5, Q5a).
 *
 * The lead's own events are any agent's. What its members do comes wrapped, so nothing
 * that reads the lead's events mistakes a member's for the lead's:
 *   member_started  {member_id, member_name, agent_id, task, card_id, model}
 *   member_event    {member_id, member_name, event}  (the member's own event, unwrapped below)
 *   member_finished {member_id, member_name, status, seconds, record}
 *   team_tasks      {tasks: [{card_id, title, member, member_id, status, result}], ended?}
 *   member_answered {member_id, question_id, approved}  (TEAM1c: a member's question settled)
 * and the answer carries `team` {tasks, tasks_used, answered_alone, spent_usd} and
 * `members` (records). A saved turn keeps both, read back with `teamFromTurn`.
 *
 * Pure: no React, no fetch — each product folds its stream through `foldTeam` and draws
 * `TeamView`.
 */
import { foldHelpers, foldLaneEvent, isHelperEvent, laneFromRecord, type HelperCall, type HelperLaneData } from '@/components/assistant/helpers';

export type MemberStatus = 'queued' | 'running' | 'waiting_for_person' | 'completed' | 'failed' | 'cancelled';

/** A member's question for a person: one tool call it may not make without an answer. */
export type MemberQuestionData = {
    /** TEAM1c's id, `{member_id}:{approval_id}`; the same made here when the event has none. */
    questionId: string;
    approvalId: string;
    toolName: string;
    toolArgs: Record<string, unknown> | null;
    category?: string;
    prompt?: string;
    /** Settled: by the person here, or elsewhere (the bell, an email's link). */
    answered?: 'approved' | 'declined';
};

export type MemberLaneData = {
    /** One assignment (`m_…`), not the agent: the same agent may work two tasks one after the other. */
    id: string;
    agentId: string;
    name: string;
    task: string;
    cardId?: string;
    model?: string;
    status: MemberStatus;
    seconds?: number;
    calls: HelperCall[];
    answer: string;
    streaming?: string;
    activity?: string;
    error?: string;
    question?: MemberQuestionData;
    /** The helpers it sent, folded as an agent's are. */
    helpers: HelperLaneData[];
};

export type TeamTaskStatus = 'claimed' | 'done' | 'failed';

export type TeamTask = {
    cardId: string;
    title: string;
    member: string;
    memberId: string;
    status: TeamTaskStatus | string;
    result?: string;
};

/** One exchange, as one line: who wrote to whom. Drawn from the calls that carry it, and the person's own. */
export type TeamNote = {
    id: string;
    from: string;
    to: string;
    text: string;
    /** `person`: the person's own words, sent from the composer. */
    by: 'lead' | 'member' | 'person';
};

export type TeamSummary = {
    tasksUsed: number;
    answeredAlone: boolean;
    spentUsd?: number;
};

export type TeamState = {
    tasks: TeamTask[];
    members: MemberLaneData[];
    notes: TeamNote[];
    /** The lead's turn has ended: nobody works on. */
    ended: boolean;
    summary?: TeamSummary;
};

/** An event or a record as the wire has it: read field by field below. */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Wire = Record<string, any>;

/** The lead's own tools in team mode: its lanes and its task list already show what each did. */
export const LEAD_TOOLS = ['assign', 'message', 'wait', 'forward'] as const;

export function emptyTeam(): TeamState {
    return { tasks: [], members: [], notes: [], ended: false };
}

/** Whether an event is a team's: `foldTeam` reads it, and the lead's own reader leaves it alone. */
export function isTeamEvent(event: Wire): boolean {
    const type = event?.type;
    return type === 'member_started' || type === 'member_event' || type === 'member_finished'
        || type === 'team_tasks' || type === 'member_answered';
}

/** Whether a team did anything a person would look for: a member, a task or a note. */
export function hasTeam(state: TeamState | null | undefined): state is TeamState {
    return !!state && (state.members.length > 0 || state.tasks.length > 0 || state.notes.length > 0);
}

const STATUSES: MemberStatus[] = ['queued', 'running', 'waiting_for_person', 'completed', 'failed', 'cancelled'];

function statusOf(value: unknown, fallback: MemberStatus): MemberStatus {
    return STATUSES.includes(value as MemberStatus) ? (value as MemberStatus) : fallback;
}

/** A member's lane from its saved record (a reopened turn, `member_finished`, or the answer's `members`). */
export function memberFromRecord(record: Wire): MemberLaneData {
    const lane = laneFromRecord(record);
    return {
        id: lane.id,
        agentId: String(record.agent_id ?? ''),
        name: String(record.name ?? 'Member'),
        task: lane.task,
        cardId: record.card_id ? String(record.card_id) : undefined,
        model: lane.model,
        status: statusOf(record.status, 'completed'),
        seconds: lane.seconds,
        calls: lane.calls,
        answer: lane.answer,
        error: lane.error,
        helpers: [],
    };
}

function taskFrom(wire: Wire): TeamTask {
    return {
        cardId: String(wire.card_id ?? ''),
        title: String(wire.title ?? ''),
        member: String(wire.member ?? ''),
        memberId: String(wire.member_id ?? ''),
        status: String(wire.status ?? 'claimed'),
        ...(wire.result ? { result: String(wire.result) } : {}),
    };
}

/** A question as a member's `human_input_required` has it, for the member it came from. */
export function questionFrom(event: Wire, memberId: string): MemberQuestionData {
    const approvalId = String(event.approval_id ?? '');
    return {
        questionId: String(event.question_id ?? `${memberId}:${approvalId}`),
        approvalId,
        toolName: String(event.tool_name ?? ''),
        toolArgs: event.tool_args && typeof event.tool_args === 'object' ? event.tool_args : null,
        ...(event.category ? { category: String(event.category) } : {}),
        ...(event.prompt ? { prompt: String(event.prompt) } : {}),
    };
}

function update(state: TeamState, id: string, change: (lane: MemberLaneData) => MemberLaneData): TeamState {
    if (!state.members.some((m) => m.id === id)) return state;
    return { ...state, members: state.members.map((m) => (m.id === id ? change(m) : m)) };
}

function note(state: TeamState, item: Omit<TeamNote, 'id'>): TeamState {
    if (!item.text.trim()) return state;
    return { ...state, notes: [...state.notes, { ...item, id: `n${state.notes.length + 1}` }] };
}

/** The person's own words to the lead or a member, sent from the composer: a note like the rest. */
export function personNote(state: TeamState, to: string, text: string): TeamState {
    return note(state, { from: 'You', to, text, by: 'person' });
}

/** A member's lane after one of its own events (unwrapped from `member_event`). */
function foldMemberEvent(lane: MemberLaneData, inner: Wire): MemberLaneData {
    if (isHelperEvent(inner)) return { ...lane, helpers: foldHelpers(lane.helpers, inner) };
    if (inner.type === 'human_input_required') {
        const question = questionFrom(inner, lane.id);
        // The same question again (a resumed turn sends each one still waiting): keep what is known of it.
        if (lane.question?.questionId === question.questionId) return { ...lane, status: 'waiting_for_person' };
        return { ...lane, status: 'waiting_for_person', question, activity: undefined };
    }
    const next = foldLaneEvent(lane, inner);
    // Its asked call has a result with no answer seen here, so nobody waits on that question: it was settled
    // without this page (TEAM1a's interim refuses a second member's question at once), not declined by the
    // person. A question answered here or announced (`member_answered`) keeps its answer.
    if (inner.type === 'tool_result' && lane.question && inner.tool_name === lane.question.toolName) {
        const status = lane.status === 'waiting_for_person' ? 'running' : lane.status;
        return { ...next, status, question: lane.question.answered ? lane.question : undefined };
    }
    return next;
}

/** The team after one event. Events of a member not seen starting are ignored; any other event is. */
export function foldTeam(state: TeamState, event: Wire): TeamState {
    switch (event?.type) {
        case 'member_started': {
            const id = String(event.member_id ?? '');
            if (!id || state.members.some((m) => m.id === id)) return state;
            return {
                ...state,
                members: [...state.members, {
                    id, agentId: String(event.agent_id ?? ''), name: String(event.member_name || 'Member'),
                    task: String(event.task ?? ''), cardId: event.card_id ? String(event.card_id) : undefined,
                    model: event.model ? String(event.model) : undefined,
                    status: 'running', calls: [], answer: '', streaming: '', helpers: [],
                }],
            };
        }
        case 'member_event': {
            const inner: Wire = event.event || {};
            const id = String(event.member_id ?? '');
            let next = update(state, id, (lane) => foldMemberEvent(lane, inner));
            if (inner.type === 'tool_execution' && inner.tool_name === 'message' && inner.args) {
                const from = String(event.member_name || 'A member');
                const to = String(inner.args.to ?? '');
                next = note(next, { from, to: to.toLowerCase() === 'lead' ? 'the lead' : to,
                                    text: String(inner.args.text ?? ''), by: 'member' });
            }
            return next;
        }
        case 'member_finished': {
            const id = String(event.member_id ?? '');
            const record = event.record ? memberFromRecord(event.record) : null;
            return update(state, id, (lane) => ({
                ...lane,
                ...(record ? { calls: record.calls.length ? record.calls : lane.calls, answer: record.answer,
                               error: record.error, seconds: record.seconds, model: record.model ?? lane.model } : {}),
                status: statusOf(event.status, record?.status ?? lane.status),
                seconds: typeof event.seconds === 'number' ? event.seconds : record?.seconds ?? lane.seconds,
                streaming: '', activity: undefined,
            }));
        }
        case 'team_tasks':
            return { ...state, tasks: Array.isArray(event.tasks) ? event.tasks.map(taskFrom) : state.tasks,
                     ended: state.ended || event.ended === true };
        case 'member_answered': {
            const id = String(event.member_id ?? '');
            return update(state, id, (lane) => {
                if (!lane.question) return lane;
                if (event.question_id && String(event.question_id) !== lane.question.questionId) return lane;
                return { ...lane, status: lane.status === 'waiting_for_person' ? 'running' : lane.status,
                         question: { ...lane.question, answered: event.approved ? 'approved' : 'declined' } };
            });
        }
        case 'human_input_required': {
            // A paused turn's question, asked by a member: drawn in that member's lane (the lead's own
            // questions and a one-at-a-time delegate's have no member and stay the page's card).
            const id = String(event.member_id ?? '');
            if (!id) return state;
            return update(state, id, (lane) => foldMemberEvent(lane, event));
        }
        case 'tool_execution': {
            // The lead's message to a member, as one line; its other calls are the lanes and the task list.
            if (event.delegate_from || event.tool_name !== 'message' || !event.args || !state.members.length) {
                return state;
            }
            return note(state, { from: 'the lead', to: String(event.args.to ?? ''), text: String(event.args.text ?? ''),
                                 by: 'lead' });
        }
        case 'response':
        case 'final':
            return withAnswer(state, event);
        default:
            return state;
    }
}

/** The team as its answer leaves it: the summary, and each member's record (its whole answer, its calls). */
export function withAnswer(state: TeamState, answer: Wire): TeamState {
    const team: Wire | undefined = answer?.team;
    const records: Wire[] = Array.isArray(answer?.members) ? answer.members : [];
    if (!team && !records.length) return state;
    let members = state.members;
    for (const record of records) {
        const kept = memberFromRecord(record);
        members = members.some((m) => m.id === kept.id)
            ? members.map((m) => (m.id === kept.id
                ? { ...m, ...kept, calls: kept.calls.length ? kept.calls : m.calls, helpers: m.helpers,
                    question: m.question, streaming: '', activity: undefined }
                : m))
            : [...members, kept];
    }
    return {
        ...state,
        members,
        tasks: Array.isArray(team?.tasks) ? team.tasks.map(taskFrom) : state.tasks,
        ended: true,
        ...(team ? { summary: summaryFrom(team) } : {}),
    };
}

function summaryFrom(team: Wire): TeamSummary {
    return {
        tasksUsed: Number(team.tasks_used ?? 0) || 0,
        answeredAlone: team.answered_alone === true,
        ...(typeof team.spent_usd === 'number' ? { spentUsd: team.spent_usd } : {}),
    };
}

/** A saved turn's team (`members`, `team`), or null when it had none. */
export function teamFromTurn(turn: Wire | null | undefined): TeamState | null {
    if (!turn || (!turn.team && !(Array.isArray(turn.members) && turn.members.length))) return null;
    return withAnswer(emptyTeam(), { team: turn.team, members: turn.members });
}

/** Members still at work: those a person can write to or stop. */
export function workingMembers(state: TeamState | null | undefined): MemberLaneData[] {
    if (!state || state.ended) return [];
    return state.members.filter((m) => m.status === 'running' || m.status === 'queued'
        || m.status === 'waiting_for_person');
}

/** The person answered a member's question here: its lane says so at once, before the stream does. */
export function answeredHere(state: TeamState, memberId: string, approved: boolean): TeamState {
    return update(state, memberId, (lane) => (lane.question
        ? { ...lane, question: { ...lane.question, answered: approved ? 'approved' : 'declined' } }
        : lane));
}

/** A question waiting on the person, by its member. */
export function waitingQuestions(state: TeamState | null | undefined): MemberLaneData[] {
    if (!state) return [];
    return state.members.filter((m) => m.question && !m.question.answered);
}

/** "1 min 40 s", "12 s". */
export function duration(seconds?: number): string {
    if (seconds === undefined || seconds < 0) return '';
    const whole = Math.round(seconds);
    if (whole < 60) return `${whole} s`;
    const minutes = Math.floor(whole / 60);
    const rest = whole % 60;
    return rest ? `${minutes} min ${rest} s` : `${minutes} min`;
}

/** A lane's one status word or two: "working", "needs you", "done · 41 s". */
export function memberStatusLine(lane: MemberLaneData, { stopping = false } = {}): string {
    const took = duration(lane.seconds);
    switch (lane.status) {
        case 'queued':
            return 'waiting for a free slot';
        case 'running':
            if (stopping) return 'stopping…';
            return lane.activity || (lane.streaming ? 'writing' : 'working');
        case 'waiting_for_person':
            return lane.question?.answered ? 'carrying on' : 'needs you';
        case 'completed':
            return took ? `done · ${took}` : 'done';
        case 'cancelled':
            return took ? `stopped · ${took}` : 'stopped';
        default:
            return lane.error ? `failed: ${lane.error}` : 'failed';
    }
}

/** "3 of 6 tasks", or "3 tasks" when the budget is not known here. */
export function taskCount(state: TeamState, budget?: number | null): string {
    const used = Math.max(state.tasks.length, state.summary?.tasksUsed ?? 0);
    if (budget) return `${used} of ${budget} task${budget === 1 ? '' : 's'}`;
    return `${used} task${used === 1 ? '' : 's'}`;
}

/** A dollar amount the way the turn's line says it. */
export function dollars(usd: number): string {
    if (usd <= 0) return '$0.00';
    if (usd < 0.01) return 'under $0.01';
    return `$${usd.toFixed(2)}`;
}

/**
 * Under a Team agent's answer (Q1b): it answered alone, or how many members and tasks it took and what the
 * models cost. Null for a turn with no team.
 */
export function teamSummaryLine(state: TeamState | null | undefined): string | null {
    const summary = state?.summary;
    if (!summary) return null;
    const cost = summary.spentUsd !== undefined && summary.spentUsd > 0 ? dollars(summary.spentUsd) : '';
    if (summary.answeredAlone) return ['Answered alone, without its members', cost].filter(Boolean).join(' · ');
    const names = new Set(state!.members.map((m) => m.agentId || m.name));
    const people = names.size ? `${names.size} member${names.size === 1 ? '' : 's'}` : '';
    const tasks = `${summary.tasksUsed} task${summary.tasksUsed === 1 ? '' : 's'}`;
    return [people, tasks, cost].filter(Boolean).join(' · ');
}

/**
 * A Team agent's "Tools used" without the lead's own team tools: its task list and its lanes already show what
 * each assign, message, wait and forward did. Names in, names out.
 */
export function toolsBesideTeam(tools: string[], state: TeamState | null | undefined): string[] {
    if (!hasTeam(state)) return tools;
    return tools.filter((t) => !(LEAD_TOOLS as readonly string[]).includes(t));
}
