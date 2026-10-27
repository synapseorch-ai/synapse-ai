/**
 * Synapse AI's chats, each its own (TABS2, D327; F1061).
 *
 * Synapse AI held one conversation for the whole app: the panel and the chat page's one Synapse AI tab drew the same
 * state. A chat is now its client id, the `client_session_id` its turns run under, and each lives here as a small
 * store of its own: its messages, its question, its one stream. The panel shows one, a Synapse AI tab shows another,
 * and the same id in both is the same store, so a chat begun in the panel carries on in a tab, or the other way.
 *
 * A chat is kept while something shows it. When nothing does, its stream is dropped and so is the store; the run goes
 * on on the server, and coming back reads the chat again (`GET /api/builder/conversation?session=`), following a turn
 * still working, as a reload always has (F214). That keeps to TABS1's rule that only what is on screen streams.
 */
import {
    applyBuilderEvent,
    emptyBuilderState,
    refusedAnswer,
    SET_ASIDE_NOTE,
    typedWhilePending,
    type BuilderMessage,
    type BuilderState,
    type SavedResource,
} from '@/lib/builderEvents';
import { isPanelEvent, reattachedState, type OpenConversation } from '@/lib/builderReattach';
import { readWithStallTimeout } from '@/lib/sse';

/**
 * How long Stop waits for the run's own cancel event before it stops waiting.
 * The run ends at its next step boundary, which is immediate unless a provider
 * call is holding it — and that is exactly the case Stop exists for.
 */
const STOP_GRACE_MS = 5000;

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type RunEvent = Record<string, any>;

/** What a chat's `send` takes beyond its words. */
export interface SendOptions {
    /** The words answer a waiting plan itself — its card's buttons, or typed after Change something (F759). Without
     * it, words typed while a plan waits are a new request, and the plan is set aside. */
    revising?: boolean;
    /** The failed run a "Try again on a stronger model" press tries again (F1018); the server picks the model, and
     * the next message is on the usual one. */
    retryOf?: string;
}

/** What a turn takes from the app around the chat, read when it is sent. */
export interface SendContext {
    model: string;
    currentOrchestrationId: string | null;
}

/** A chat as its views read it. */
export interface ChatView {
    id: string;
    state: BuilderState;
    streaming: boolean;
    /** The chat has been read back, or needed no reading: until then a view draws neither greeting nor turns. */
    ready: boolean;
}

/** The conversation before the person's last message: what a re-run of that message is sent with (F1018). */
function beforeLastAsked(messages: BuilderMessage[]): BuilderMessage[] {
    const at = messages.map((m) => m.role).lastIndexOf('user');
    return at < 0 ? messages : messages.slice(0, at);
}

export function newChatId(): string {
    return typeof crypto !== 'undefined' && crypto.randomUUID
        ? crypto.randomUUID()
        : String(Date.now());
}

/**
 * A journal event shrunk past 32 KB, completed from the run itself. The journal
 * marks what it cut `truncated: true`, and may have cut a completion's summary
 * or a plan down to 2,000 characters, or to its type alone; the run's own row
 * holds the whole of both.
 */
async function fromTheRun(runId: string, event: RunEvent): Promise<RunEvent> {
    try {
        const response = await fetch(`/api/orchestrations/runs/${encodeURIComponent(runId)}`);
        if (!response.ok) return event;
        const run = await response.json();
        if (event.type === 'orchestration_complete') {
            return { ...event, run_id: runId, final_state: run.shared_state ?? {} };
        }
        if (event.type === 'human_input_required') {
            return {
                ...event, run_id: runId,
                prompt: run.human_prompt ?? event.prompt,
                fields: run.human_fields ?? event.fields,
                orch_step_id: event.orch_step_id ?? run.current_step_id,
            };
        }
    } catch {
        /* the shrunk event is still better than none */
    }
    return event;
}

/** Ask the server to end a run. The POST is the whole of it. */
export async function cancelRun(runId: string): Promise<void> {
    const response = await fetch(`/api/orchestrations/runs/${encodeURIComponent(runId)}/cancel`, { method: 'POST' });
    if (!response.ok) throw new Error(`The run could not be stopped (${response.status}).`);
}

/** One Synapse AI chat: its state, its one stream, and what it can be asked to do. */
export class BuilderChat {
    readonly id: string;
    private view: ChatView;
    private listeners = new Set<() => void>();
    private abort: AbortController | null = null;
    /** Set by `stop`, so an aborted stream reads as stopped rather than as a fault. */
    private stopRequested = false;
    /** Whether a message has been sent here: a read-back answering after it must not replace it. */
    private touched = false;
    private closed = false;

    constructor(
        id: string,
        private readonly welcome: BuilderMessage,
        private readonly announce: (saved: SavedResource) => void,
        /** Nothing to read back: a chat started here, not one picked from a list. */
        fresh: boolean,
    ) {
        this.id = id;
        this.view = { id, state: emptyBuilderState([welcome]), streaming: false, ready: fresh };
        if (!fresh) void this.readBack();
    }

    subscribe = (listener: () => void): (() => void) => {
        this.listeners.add(listener);
        return () => { this.listeners.delete(listener); };
    };

    getSnapshot = (): ChatView => this.view;

    private set(change: Partial<ChatView>): void {
        if (this.closed) return;
        this.view = { ...this.view, ...change };
        this.listeners.forEach((listener) => listener());
    }

    private update(apply: (prev: BuilderState) => BuilderState): void {
        this.set({ state: apply(this.view.state) });
    }

    /**
     * Take this chat's earlier turns from its runs, and follow a turn still working. The chat must not already have
     * been written to here: a message sent before the read answered is the newer truth.
     */
    private async readBack(): Promise<void> {
        try {
            const response = await fetch(`/api/builder/conversation?session=${encodeURIComponent(this.id)}`);
            if (!response.ok || this.touched || this.closed) return;
            const { conversation } = (await response.json()) as { conversation: OpenConversation | null };
            if (this.touched || this.closed) return;
            if (conversation) this.adopt(conversation);
        } catch {
            /* the chat opens on its greeting */
        } finally {
            this.set({ ready: true });
        }
    }

    /** Draw a chat read back from its runs, and follow its turn if it is still working (F214). */
    adopt(conversation: OpenConversation): void {
        this.touched = true;
        this.set({ state: reattachedState(conversation, this.welcome), ready: true });
        if (conversation.status === 'running') void this.follow(conversation.run_id, Number(conversation.cursor ?? 0) || 0);
    }

    /** Whether nothing has been said in it yet, here or before. */
    isBlank(): boolean {
        return !this.touched && this.view.state.messages.every((m) => m.key === 'welcome');
    }

    /** Drop the stream and stop telling anyone anything. The run goes on on the server. */
    close(): void {
        this.abort?.abort();
        this.closed = true;
        this.listeners.clear();
    }

    /**
     * Read a run's events into the chat.
     *
     * `following` is set when the stream is the run's journal rather than the
     * response to a message (the chat was read back while a turn worked). The
     * journal stays open across a pause, where this product answers through
     * `/api/builder/resume`'s own stream, so reading stops at the question the
     * run is asking now — the journal replays the ones already answered, which
     * sit at or before `following.cursor`. It also shrinks an event past 32 KB,
     * so a shrunk completion or question is re-read from the run itself.
     */
    private async consume(response: Response, following?: { runId: string; cursor: number }): Promise<'paused' | 'ended'> {
        const reader = response.body?.getReader();
        if (!reader) return 'ended';
        const decoder = new TextDecoder();
        let buffer = '';
        let seq = 0;
        // The journal's id for the event that follows (`id: N`).
        let eventId: number | null = null;

        for (;;) {
            const { done, value } = await readWithStallTimeout(reader, this.abort!);
            if (done) break;
            buffer += decoder.decode(value, { stream: true });
            const lines = buffer.split('\n');
            buffer = lines.pop() ?? '';
            for (const line of lines) {
                if (line.startsWith('id: ')) {
                    eventId = Number(line.slice(4));
                    continue;
                }
                if (!line.startsWith('data: ')) continue;
                const thisId = eventId;
                eventId = null;
                let event: unknown;
                try {
                    event = JSON.parse(line.slice(6));
                } catch {
                    continue; // a heartbeat or a half-written frame
                }
                let parsed = event as RunEvent;
                if (!isPanelEvent(parsed?.type)) continue;
                if (following && parsed.truncated) parsed = await fromTheRun(following.runId, parsed);
                seq += 1;
                const n = seq;
                this.update((prev) => applyBuilderEvent(prev, parsed, n));
                if (parsed?.type === 'resource_saved') this.announce(parsed as SavedResource);
                if (following && parsed?.type === 'human_input_required') {
                    // Answered before the chat was read: history.
                    const replayed = thisId !== null && Number.isFinite(thisId) && thisId <= following.cursor;
                    if (!replayed) {
                        await reader.cancel().catch(() => undefined);
                        return 'paused';
                    }
                }
            }
        }
        return 'ended';
    }

    /** A failed or stopped stream, said in the chat. */
    private reportStreamEnd(error: unknown): void {
        const aborted = (error as Error)?.name === 'AbortError';
        // Stop gave up waiting for the run's own cancel event. Say so
        // here, because nothing else will now.
        if (aborted && this.stopRequested) {
            this.update((prev) => ({
                ...prev,
                status: null, pending: null, done: true, error: null,
                messages: [
                    ...prev.messages,
                    { role: 'assistant', content: 'Stopped.', kind: 'text', key: `stopped-${prev.messages.length}` },
                ],
            }));
        } else if (!aborted) {
            const message = (error as Error)?.message ?? 'Something went wrong.';
            this.update((prev) => ({
                ...prev,
                error: message,
                messages: [
                    ...prev.messages,
                    { role: 'assistant', content: `**${message}**`, kind: 'text', key: `error-${prev.messages.length}` },
                ],
            }));
        }
    }

    /** Follow a run this chat did not start here, from its journal's beginning (F214). */
    private async follow(runId: string, cursor: number): Promise<void> {
        const controller = new AbortController();
        this.abort = controller;
        this.set({ streaming: true });
        try {
            const response = await fetch(
                `/api/orchestrations/runs/${encodeURIComponent(runId)}/events/stream?after=0`,
                { signal: controller.signal },
            );
            if (!response.ok) throw new Error(`The build could not be followed (${response.status}).`);
            await this.consume(response, { runId, cursor });
        } catch (error) {
            this.reportStreamEnd(error);
        } finally {
            this.set({ streaming: false });
            if (this.abort === controller) this.abort = null;
            this.stopRequested = false;
        }
    }

    send = async (text: string, images: string[] = [], options: SendOptions = {}, context?: SendContext): Promise<void> => {
        const { state, streaming } = this.view;
        // An image on its own is a message. Only an empty one is nothing.
        if ((!text.trim() && !images.length) || streaming) return;
        this.touched = true;
        // A new request while a plan waits (F759): the plan is stopped where it waits — nothing it planned was
        // done — and this message starts a new turn instead of answering it.
        const setAside = state.pending !== null && typedWhilePending(state.pending, text, options.revising ?? false) === 'set_aside';
        const pending = setAside ? null : state.pending;
        if (setAside && state.pending) await cancelRun(state.pending.runId).catch(() => undefined);

        const { retryOf } = options;
        // An offer is for the turn it follows: any new message takes it down. A press says what it tries instead of
        // the message again, which is already on screen (F1018).
        const offered = retryOf ? state.messages.find((m) => m.tryStronger?.retry_of === retryOf)?.tryStronger : null;
        this.update((prev) => ({
            ...prev,
            pending: null,
            done: false,
            error: null,
            messages: [
                ...prev.messages.filter((m) => m.kind !== 'try_stronger'),
                ...(offered
                    ? [{ role: 'assistant' as const, content: `Trying again on ${offered.name}…`, kind: 'text' as const,
                         key: `retry-${retryOf}` }]
                    : []),
                ...(retryOf ? [] : setAside
                    ? [{ role: 'assistant' as const, content: SET_ASIDE_NOTE, kind: 'text' as const,
                         key: `aside-${prev.messages.length}` }]
                    : []),
                ...(retryOf ? [] : [{
                    role: 'user' as const,
                    content: text || `(${images.length} ${images.length === 1 ? 'image' : 'images'})`,
                    kind: 'text' as const,
                    key: `user-${prev.messages.length}`,
                }]),
            ],
        }));
        this.set({ streaming: true });

        const controller = new AbortController();
        this.abort = controller;
        try {
            // Answering an open question resumes the paused run; anything
            // else starts a new one.
            const response = pending
                ? await fetch('/api/builder/resume', {
                      method: 'POST',
                      headers: { 'Content-Type': 'application/json' },
                      signal: controller.signal,
                      body: JSON.stringify({
                          run_id: pending.runId,
                          response: { [pending.field]: text },
                          images,
                      }),
                  })
                : await fetch('/api/builder/chat', {
                      method: 'POST',
                      headers: { 'Content-Type': 'application/json' },
                      signal: controller.signal,
                      body: JSON.stringify({
                          message: text,
                          // A press re-runs the failed turn, so its message is not in its own history.
                          history: (retryOf ? beforeLastAsked(state.messages) : state.messages)
                              .filter((m) => m.kind === 'text' && m.key !== 'welcome')
                              .map((m) => ({ role: m.role, content: m.content })),
                          can_create_agents: true,
                          model: context?.model || undefined,
                          images,
                          current_orchestration_id: context?.currentOrchestrationId ?? null,
                          client_session_id: this.id,
                          // A schedule the person names no timezone for runs on their own clock (F948).
                          timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
                          ...(retryOf ? { retry_of: retryOf } : {}),
                      }),
                  });

            if (!response.ok) throw new Error(`The builder could not be reached (${response.status}).`);
            await this.consume(response);
            // A refused answer leaves its question open (BWA1): the run is
            // still parked on it, so the next message answers it again.
            if (pending) this.update((prev) => (refusedAnswer(prev) && !prev.pending ? { ...prev, pending } : prev));
        } catch (error) {
            this.reportStreamEnd(error);
        } finally {
            this.set({ streaming: false });
            this.abort = null;
            this.stopRequested = false;
        }
    };

    /**
     * Stop the chat's run.
     *
     * The POST is what ends it; nothing is aborted first, because cutting the
     * stream would drop the very event that says what happened. A run being
     * built is followed by an open stream, so the engine's own cancel arrives
     * on it and the reducer renders "Stopped." — with the timer below as the
     * backstop for a run wedged inside a provider call.
     *
     * A run parked on a question has **no** open stream: this product ends a
     * run's stream at the pause rather than holding it. So nothing will report
     * that cancel, and this says it itself.
     */
    stop = async (): Promise<void> => {
        const { state } = this.view;
        const runId = state.pending?.runId ?? state.runId;
        if (!runId) return;

        const following = this.abort;
        this.stopRequested = true;
        this.update((prev) => ({ ...prev, status: 'Stopping…' }));
        let ended = true;
        try {
            await cancelRun(runId);
        } catch {
            // Already finished, or unreachable. A run being followed still has
            // the timer below; a parked one is reported as it went.
            ended = false;
        }

        if (!following) {
            this.stopRequested = false;
            this.update((prev) => ({
                ...prev,
                status: null,
                // The question stays open when the run did: it is still the
                // one thing this chat can do.
                ...(ended ? { pending: null, done: true, error: null } : {}),
                messages: [
                    ...prev.messages,
                    {
                        role: 'assistant',
                        content: ended ? 'Stopped.' : '**The run could not be stopped.**',
                        kind: 'text',
                        key: `stopped-${prev.messages.length}`,
                    },
                ],
            }));
            return;
        }
        window.setTimeout(() => {
            if (this.abort === following) following.abort();
        }, STOP_GRACE_MS);
    };
}

/**
 * Every chat something shows, by id. A view takes a chat while it shows it and lets it go after; the last to let go
 * closes it, a tick later, so a view that lets go and takes it straight back (React's Strict Mode, a re-key) keeps it.
 */
export class BuilderChats {
    private chats = new Map<string, { chat: BuilderChat; holders: number; closing: number | null }>();

    constructor(
        private readonly welcome: BuilderMessage,
        private readonly announce: (saved: SavedResource) => void,
    ) {}

    /** The chat `id`, made if nothing holds it yet; `fresh` says there is nothing on the server to read back. */
    get(id: string, fresh = false): BuilderChat {
        let held = this.chats.get(id);
        if (!held) {
            held = { chat: new BuilderChat(id, this.welcome, this.announce, fresh), holders: 0, closing: null };
            this.chats.set(id, held);
        }
        return held.chat;
    }

    /** Hold `id` while a view shows it; the returned function lets it go. */
    hold(id: string, fresh = false): () => void {
        this.get(id, fresh);
        const held = this.chats.get(id)!;
        held.holders += 1;
        if (held.closing !== null) { window.clearTimeout(held.closing); held.closing = null; }
        return () => {
            held.holders -= 1;
            if (held.holders > 0) return;
            held.closing = window.setTimeout(() => {
                if (held.holders > 0 || this.chats.get(id) !== held) return;
                held.chat.close();
                this.chats.delete(id);
            }, 0);
        };
    }
}
