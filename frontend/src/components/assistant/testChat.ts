/*
 * The agents workbench's test chat, as data (M2d, D211).
 *
 * One reducer for both products: each product reads the turn's events from its
 * own transport (open source streams the POST, cloud follows a stream URL) and
 * folds them here. The events are the engine's, the same in both; the
 * self-hosted route renames `final` to `response` and carries the answer as
 * `content`, so both spellings are read.
 *
 * Deliberately smaller than either chat's reducer: the panel answers "what does
 * this agent do with that question", so it keeps the agent's own calls and its
 * answer, and says when a turn stops for a person instead of answering it here
 * (that is chat's job, and the conversation is in chat's history).
 */
import type { KnowledgeCitation } from '@/lib/knowledge';

export interface TestCall {
    key: string;
    /** The dispatch name, as a step stores it and a run log prints it. */
    name: string;
    args: Record<string, unknown> | null;
    /** The tool's result preview; `null` while it runs. */
    result: string | null;
    /** Not run: refused by the tool policy or the guard. */
    refused?: boolean;
    reason?: string;
    failed?: boolean;
}

export type TestTurnState = 'streaming' | 'done' | 'failed' | 'cancelled' | 'waiting';

export interface TestTurn {
    id: string;
    question: string;
    /** How many images went with the question (open source keeps none after the turn). */
    images?: number;
    calls: TestCall[];
    answer: string;
    /** What is happening now, while it streams: "Calling search_knowledge…". */
    status: string;
    state: TestTurnState;
    error?: string;
    /** Stopped for a person: a tool call to approve, or a workflow's question. */
    waiting?: 'approval' | 'input';
    citations?: KnowledgeCitation[];
    unresolved?: string[];
}

/** An event as either product's stream delivers it. */
export type TestEvent = { type?: string } & Record<string, unknown>;

export function newTurn(id: string, question: string, images = 0): TestTurn {
    return { id, question, ...(images ? { images } : {}), calls: [], answer: '', status: '', state: 'streaming' };
}

/** Whether a `tool_result` preview is the engine reporting a failure (cloud's `turn.ts` reads it the same way). */
function isFailure(preview: string): boolean {
    return /^(error|blocked|tool removed|tool not found)/i.test(preview.trim());
}

const text = (v: unknown) => (typeof v === 'string' ? v : v == null ? '' : String(v));

/**
 * Fold one event into the turn. Returns the same object when nothing changed.
 *
 * A sub-agent's events (`delegate_from`) and a workflow step's (`orch_step_id`)
 * move the status line only: the calls listed are the tested agent's own, and a
 * step's output is not the workflow's answer.
 */
export function reduceTest(turn: TestTurn, event: TestEvent): TestTurn {
    const nested = Boolean(event.delegate_from || event.orch_step_id);
    const who = event.delegate_from
        ? text(event.delegate_agent_name ?? event.delegate_from)
        : event.orch_step_id ? text(event.step_name) : '';
    switch (event.type) {
        case 'status':
        case 'thinking': {
            const message = text(event.message);
            if (!message) return turn;
            return { ...turn, status: who ? `${who} · ${message}` : message };
        }
        case 'step_start':
            return { ...turn, status: text(event.step_name) ? `${text(event.step_name)}…` : turn.status };
        case 'tool_execution': {
            const name = text(event.tool_name) || 'tool';
            if (nested) return { ...turn, status: `${who ? `${who} · ` : ''}Calling ${name}…` };
            const args = event.args && typeof event.args === 'object' ? (event.args as Record<string, unknown>) : null;
            return {
                ...turn,
                status: `Calling ${name}…`,
                calls: [...turn.calls, { key: `${name}:${turn.calls.length}`, name, args, result: null }],
            };
        }
        case 'tool_result': {
            if (nested) return turn;
            const name = text(event.tool_name) || 'tool';
            const preview = text(event.preview);
            // The newest call to this tool still without its result: tools run one
            // at a time in the loop, so the pairing is unambiguous.
            for (let i = turn.calls.length - 1; i >= 0; i -= 1) {
                const call = turn.calls[i];
                if (call.name !== name || call.result !== null) continue;
                const calls = [...turn.calls];
                calls[i] = event.refused === true
                    ? { ...call, result: preview, refused: true, reason: text(event.reason) }
                    : { ...call, result: preview, failed: isFailure(preview) };
                return { ...turn, calls, status: '' };
            }
            return turn;
        }
        case 'knowledge_citations': {
            if (nested) return turn;
            const citations = Array.isArray(event.citations) ? (event.citations as KnowledgeCitation[]) : [];
            const unresolved = Array.isArray(event.unresolved) ? (event.unresolved as string[]) : [];
            if (!citations.length && !unresolved.length) return turn;
            return { ...turn, citations, unresolved };
        }
        case 'final':
        case 'response': {
            if (event.orch_step_id) return turn;
            return { ...turn, answer: text(event.response ?? event.content), status: '', state: 'done' };
        }
        case 'human_input_required':
            return {
                ...turn,
                status: '',
                state: 'waiting',
                waiting: event.kind === 'tool_approval' ? 'approval' : 'input',
            };
        case 'cancelled':
            return { ...turn, status: '', state: 'cancelled' };
        case 'error':
        case 'orchestration_error':
            return { ...turn, status: '', state: 'failed', error: text(event.message ?? event.error) || 'Something went wrong.' };
        case 'done':
            // `done` follows `cancelled` and `human_input_required` on the wire; it
            // must not undo either.
            return turn.state === 'streaming' ? { ...turn, status: '', state: 'done' } : turn;
        default:
            return turn;
    }
}

/** A stream that closed before its turn ended: the turn says so rather than spinning. */
export function endTest(turn: TestTurn, reason: string): TestTurn {
    return turn.state === 'streaming' ? { ...turn, status: '', state: 'failed', error: reason } : turn;
}

/**
 * The saved agent in one line, for the panel's head: "gpt-5 · 3 tools · Order log".
 * `tools` are already the names a person reads; an agent with every tool says so.
 */
export function agentContext({ model, tools, knowledge }: {
    model?: string | null;
    tools: string[] | 'all';
    knowledge: string[];
}): string {
    const parts = [model || 'Default model'];
    parts.push(tools === 'all' ? 'every tool' : tools.length === 0 ? 'no tools' : tools.length === 1 ? tools[0] : `${tools.length} tools`);
    if (knowledge.length) parts.push(knowledge.length === 1 ? knowledge[0] : `${knowledge.length} knowledge bases`);
    return parts.join(' · ');
}
