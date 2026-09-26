/**
 * The AI builder panel after a reload (F214).
 *
 * The panel held its conversation in React state only. A reload mid-answer
 * lost the question and the answer while the server finished the turn; a
 * reload after *approve* lost the summary while the build landed; a reload
 * while the plan waited showed the greeting, and the run stayed paused on no
 * screen, for ever.
 *
 * `GET /api/builder/conversation` answers with the newest conversation still
 * open, read back from its run rows, and this draws it. It is open source's
 * half of the commercial edition's F148 (`web/src/lib/builderComposer.ts`),
 * kept apart from `builderEvents.ts` because that file is synced into both
 * products and this shape is not the same in both: open source answers a
 * question through `/api/builder/resume`'s own stream, so it needs no cursor.
 */
import type { BuilderMessage, BuilderState } from './builderEvents';

/** What `GET /api/builder/conversation` returns for a conversation still open. */
export interface OpenConversation {
    client_session_id: string;
    run_id: string;
    status: 'paused' | 'running';
    turns: { user: string; assistant: string }[];
    pending: { run_id: string; field: string; step_id: string | null; prompt: string } | null;
    /**
     * Where a working run's journal stood when the conversation was read. The
     * panel follows the journal from its start, which replays the questions
     * already answered; only a question past this one is being asked now.
     */
    cursor?: string;
}

/** The panel drawn from the conversation's rows; with `pending` set, the next message answers it. */
export function reattachedState(conversation: OpenConversation, welcome: BuilderMessage): BuilderState {
    const messages: BuilderMessage[] = [welcome];
    conversation.turns.forEach((turn, i) => {
        messages.push({ role: 'user', content: turn.user, kind: 'text', key: `back-user-${i}` });
        if (turn.assistant) {
            messages.push({ role: 'assistant', content: turn.assistant, kind: 'text', key: `back-said-${i}` });
        }
    });
    const asked = conversation.pending;
    if (asked) {
        messages.push({ role: 'assistant', content: asked.prompt, kind: 'human_prompt', key: `back-asked-${asked.run_id}` });
    }
    return {
        messages,
        pending: asked ? { runId: asked.run_id, field: asked.field, stepId: asked.step_id } : null,
        status: null,
        runId: conversation.run_id,
        done: false,
        error: null,
    };
}

/**
 * Whether an event from a run's journal is the panel's to draw. The journal
 * keeps the engine's own bookkeeping (`_`-prefixed) that the live stream
 * filters out, and its stream says `stream_complete` when the run has ended.
 */
export function isPanelEvent(type: unknown): boolean {
    return typeof type === 'string' && !type.startsWith('_') && type !== 'stream_complete';
}
