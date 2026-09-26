'use client';
/**
 * The AI builder, available from every screen.
 *
 * It used to be mounted inside the orchestration editor, which meant it could
 * only be reached from one screen and its conversation was destroyed by
 * navigating away. It lives in the app shell now, so the state is here.
 */
import {
    createContext,
    useCallback,
    useContext,
    useEffect,
    useMemo,
    useRef,
    useState,
} from 'react';

import {
    applyBuilderEvent,
    emptyBuilderState,
    type BuilderMessage,
    type BuilderState,
    type SavedResource,
} from '@/lib/builderEvents';
import { usePersisted } from '@/components/ui';
import { isPanelEvent, reattachedState, type OpenConversation } from '@/lib/builderReattach';
import type { ProviderModels } from '@/lib/models';
import { readWithStallTimeout } from '@/lib/sse';

/**
 * Announced when the builder writes something, so the list screens refetch
 * instead of waiting for a reload. Same shape as `NOTIFICATION_EVENT`.
 */
export const RESOURCE_SAVED_EVENT = 'synapse-resource-saved';

/**
 * How long the panel's exit transition takes. Paired with `duration-200` on the
 * card in `BuilderPanel`; kept here because this is where the timer runs.
 */
export const EXIT_MS = 200;

/**
 * How long Stop waits for the run's own cancel event before it stops waiting.
 * The run ends at its next step boundary, which is immediate unless a provider
 * call is holding it — and that is exactly the case Stop exists for.
 */
const STOP_GRACE_MS = 5000;

const WELCOME: BuilderMessage = {
    role: 'assistant',
    kind: 'text',
    key: 'welcome',
    content:
        "I build things in this workspace — agents, orchestrations, tools, skills and schedules. " +
        "Tell me what you want to happen. I'll show you a plan to approve before I build anything.\n\n" +
        // Not italicised. Three italic lines is most of what made this read as
        // filler rather than as examples you could type.
        '- "Every weekday at 8, summarise yesterday\'s support tickets and post it to Slack"\n' +
        '- "Add a tool that files a ticket in our issue tracker"\n' +
        '- "Change the research workflow to ask me before it sends anything"',
};

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type RunEvent = Record<string, any>;

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

function newSessionId(): string {
    return typeof crypto !== 'undefined' && crypto.randomUUID
        ? crypto.randomUUID()
        : String(Date.now());
}

interface OpenOptions {
    currentOrchestrationId?: string | null;
}

interface BuilderContextValue {
    isOpen: boolean;
    /**
     * Declare the orchestration a screen is showing, for as long as it shows
     * it. The builder needs a referent for "add a step to this one", and the
     * only honest source is the screen that is open: an answer stored when the
     * panel was opened outlives the screen that gave it, and the app-wide
     * launcher never had one to give (F17). Pass null on the way out.
     */
    setCurrentOrchestration: (id: string | null) => void;
    /**
     * True once the panel should be in its open position — one frame after
     * `isOpen`, and false for the length of the exit while `isOpen` is still
     * true. The panel reads this to transition; nothing else needs it.
     */
    entered: boolean;
    open: (options?: OpenOptions) => void;
    close: () => void;
    state: BuilderState;
    streaming: boolean;
    send: (text: string, images?: string[]) => Promise<void>;
    /**
     * End the attached run — the one being built, or the one parked on a
     * question. Both are the same POST; only who reports it back differs.
     */
    stop: () => Promise<void>;
    newConversation: () => void;
    providers: Record<string, ProviderModels>;
    /**
     * Whether the model this build will actually run on can see an attachment.
     * Resolved here rather than in the panel because it needs three things the
     * panel does not hold: the per-conversation pick, the workspace builder
     * model, and the workspace default behind that.
     */
    canAttach: boolean;
    builderModel: string;
    sessionModel: string;
    setSessionModel: (model: string) => void;
    saveBuilderModel: (model: string) => Promise<void>;
}

const BuilderContext = createContext<BuilderContextValue | null>(null);

export function useBuilder(): BuilderContextValue {
    const ctx = useContext(BuilderContext);
    if (!ctx) throw new Error('useBuilder must be used inside <BuilderProvider>');
    return ctx;
}

export function BuilderProvider({ children }: { children: React.ReactNode }) {
    const [isOpen, setIsOpen] = useState(false);
    const [entered, setEntered] = useState(false);
    const [state, setState] = useState<BuilderState>(() => emptyBuilderState([WELCOME]));
    const [streaming, setStreaming] = useState(false);
    const [providers, setProviders] = useState<Record<string, ProviderModels>>({});
    // Served by the backend rather than restated here: a second copy of which
    // model families can see would be wrong the first time only one was updated.
    const [visionCapable, setVisionCapable] = useState<string[]>([]);
    const [workspaceModel, setWorkspaceModel] = useState('');
    const [builderModel, setBuilderModel] = useState('');
    // Remembered, not per-conversation: picking a model and then losing it to a
    // reload or to "new conversation" made the control feel like it had not
    // taken. Empty means "use the workspace builder model".
    const [sessionModel, setSessionModel] = usePersisted<string>(
        'synapseBuilderModel', (raw) => raw ?? '', '');

    // The orchestration the user had open when they asked, so "add a step to
    // this one" means this one. Held in a ref: it is read when a message is
    // sent, and changing it should not re-render the panel.
    const currentOrchestrationId = useRef<string | null>(null);
    // Minted on first use, not during render: `crypto.randomUUID()` and
    // `Date.now()` are both impure, and a render that runs twice would
    // otherwise produce two conversations.
    const clientSessionId = useRef<string | null>(null);
    const conversationId = useCallback(() => {
        clientSessionId.current ??= newSessionId();
        return clientSessionId.current;
    }, []);
    const abortRef = useRef<AbortController | null>(null);
    /**
     * The run and the open question, readable from callbacks that must not
     * re-create themselves every time either changes.
     */
    const runIdRef = useRef<string | null>(null);
    const pendingRef = useRef<BuilderState['pending']>(null);
    /** Set by `stop`, so an aborted stream reads as stopped rather than as a fault. */
    const stopRequested = useRef(false);
    /**
     * Whether this page has used the panel yet. The conversation a reload brings
     * back (F214) is drawn only onto a panel nobody has typed into: a message
     * sent before the lookup answered must not be replaced by an older one.
     */
    const touched = useRef(false);
    //: The pending unmount at the end of the close transition, so `open` can cancel it.
    const exitTimer = useRef<number | null>(null);

    useEffect(() => {
        let cancelled = false;
        (async () => {
            try {
                const [modelsRes, settingsRes] = await Promise.all([
                    fetch('/api/models'),
                    fetch('/api/settings'),
                ]);
                if (cancelled) return;
                if (modelsRes.ok) {
                    const data = await modelsRes.json();
                    setProviders(data.providers ?? {});
                    setVisionCapable(Array.isArray(data.vision_capable) ? data.vision_capable : []);
                }
                if (settingsRes.ok) {
                    const data = await settingsRes.json();
                    setBuilderModel(data.builder_model || '');
                    setWorkspaceModel(data.model || '');
                }
            } catch {
                /* the picker falls back to the workspace default */
            }
        })();
        return () => { cancelled = true; };
    }, []);

    const setCurrentOrchestration = useCallback((id: string | null) => {
        currentOrchestrationId.current = id;
    }, []);

    const open = useCallback((options?: OpenOptions) => {
        // An explicit answer still wins, for a caller that opens the panel about
        // something other than what it is showing. Everything else comes from
        // whichever screen has registered itself.
        if (options && 'currentOrchestrationId' in options) {
            currentOrchestrationId.current = options.currentOrchestrationId ?? null;
        }
        // Cancel a close still in flight. `OrchestrationTab`'s "Build with AI"
        // stays clickable while the panel is leaving, so reopening inside the
        // exit window let the stale timer unmount what had just been reopened.
        if (exitTimer.current !== null) {
            window.clearTimeout(exitTimer.current);
            exitTimer.current = null;
        }
        setIsOpen(true);
        // Next frame, so the card paints once in its closed position before it
        // is told to open. Set both in one tick and there are no two values for
        // the transition to interpolate between.
        requestAnimationFrame(() => setEntered(true));
    }, []);

    // Closing runs the transition first and unmounts after it. This lives here
    // rather than in the panel because the provider is what owns open/close;
    // doing it in the panel would mean setState inside an effect.
    const close = useCallback(() => {
        setEntered(false);
        exitTimer.current = window.setTimeout(() => {
            exitTimer.current = null;
            setIsOpen(false);
        }, EXIT_MS);
    }, []);

    useEffect(() => {
        runIdRef.current = state.runId;
        pendingRef.current = state.pending;
    }, [state.runId, state.pending]);

    /** Ask the server to end a run. The POST is the whole of it. */
    const cancelRun = useCallback(async (runId: string) => {
        const response = await fetch(
            `/api/orchestrations/runs/${encodeURIComponent(runId)}/cancel`,
            { method: 'POST' },
        );
        if (!response.ok) throw new Error(`The run could not be stopped (${response.status}).`);
    }, []);

    const newConversation = useCallback(() => {
        touched.current = true;
        // A run parked on a question has nothing else to end it: walking away
        // leaves it paused for good. A run still *working* is left alone —
        // stopping it mid-build could strand half of what it was writing, and
        // the run is detached now, so abandoning its stream does not kill it.
        const parked = pendingRef.current?.runId;
        if (parked) void cancelRun(parked).catch(() => undefined);
        abortRef.current?.abort();
        clientSessionId.current = newSessionId();
        setState(emptyBuilderState([WELCOME]));
        setStreaming(false);
    }, [cancelRun]);

    const announce = useCallback((saved: SavedResource) => {
        window.dispatchEvent(new CustomEvent(RESOURCE_SAVED_EVENT, { detail: saved }));
    }, []);

    /**
     * Read a run's events into the panel.
     *
     * `following` is set when the stream is the run's journal rather than the
     * response to a message (a reload brought the run back, F214). The journal
     * stays open across a pause, where this product answers through
     * `/api/builder/resume`'s own stream, so reading stops at the question the
     * run is asking now — the journal replays the ones already answered, which
     * sit at or before `following.cursor`. It also shrinks an event past 32 KB,
     * so a shrunk completion or question is re-read from the run itself.
     */
    const consume = useCallback(
        async (
            response: Response,
            following?: { runId: string; cursor: number },
        ): Promise<'paused' | 'ended'> => {
            const reader = response.body?.getReader();
            if (!reader) return 'ended';
            const decoder = new TextDecoder();
            let buffer = '';
            let seq = 0;
            // The journal's id for the event that follows (`id: N`).
            let eventId: number | null = null;

            for (;;) {
                const { done, value } = await readWithStallTimeout(reader, abortRef.current!);
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
                    setState((prev) => applyBuilderEvent(prev, parsed, seq));
                    if (parsed?.type === 'resource_saved') announce(parsed as SavedResource);
                    if (following && parsed?.type === 'human_input_required') {
                        // Answered before the conversation was read: history.
                        const replayed = thisId !== null && Number.isFinite(thisId) && thisId <= following.cursor;
                        if (!replayed) {
                            await reader.cancel().catch(() => undefined);
                            return 'paused';
                        }
                    }
                }
            }
            return 'ended';
        },
        [announce],
    );

    /** A failed or stopped stream, said in the conversation. */
    const reportStreamEnd = useCallback((error: unknown) => {
        const aborted = (error as Error)?.name === 'AbortError';
        // Stop gave up waiting for the run's own cancel event. Say so
        // here, because nothing else will now.
        if (aborted && stopRequested.current) {
            setState((prev) => ({
                ...prev,
                status: null, pending: null, done: true, error: null,
                messages: [
                    ...prev.messages,
                    { role: 'assistant', content: 'Stopped.', kind: 'text', key: `stopped-${prev.messages.length}` },
                ],
            }));
        } else if (!aborted) {
            const message = (error as Error)?.message ?? 'Something went wrong.';
            setState((prev) => ({
                ...prev,
                error: message,
                messages: [
                    ...prev.messages,
                    { role: 'assistant', content: `**${message}**`, kind: 'text', key: `error-${prev.messages.length}` },
                ],
            }));
        }
    }, []);

    /** Follow a run the panel did not start, from its journal's beginning (F214). */
    const follow = useCallback(async (runId: string, cursor: number) => {
        const controller = new AbortController();
        abortRef.current = controller;
        setStreaming(true);
        try {
            const response = await fetch(
                `/api/orchestrations/runs/${encodeURIComponent(runId)}/events/stream?after=0`,
                { signal: controller.signal },
            );
            if (!response.ok) throw new Error(`The build could not be followed (${response.status}).`);
            await consume(response, { runId, cursor });
        } catch (error) {
            reportStreamEnd(error);
        } finally {
            setStreaming(false);
            if (abortRef.current === controller) abortRef.current = null;
            stopRequested.current = false;
        }
    }, [consume, reportStreamEnd]);

    // A reload brings back the conversation still open (F214): the panel is
    // redrawn from the run rows, and a run still working is followed.
    useEffect(() => {
        let cancelled = false;
        (async () => {
            try {
                const response = await fetch('/api/builder/conversation');
                if (!response.ok || cancelled || touched.current) return;
                const { conversation } = (await response.json()) as { conversation: OpenConversation | null };
                if (!conversation || cancelled || touched.current) return;
                touched.current = true;
                clientSessionId.current = conversation.client_session_id;
                setState(reattachedState(conversation, WELCOME));
                if (conversation.status === 'running') {
                    void follow(conversation.run_id, Number(conversation.cursor ?? 0) || 0);
                }
            } catch {
                /* the panel opens on its greeting, as before */
            }
        })();
        return () => { cancelled = true; };
    }, [follow]);

    const send = useCallback(
        async (text: string, images: string[] = []) => {
            // An image on its own is a message. Only an empty one is nothing.
            if ((!text.trim() && !images.length) || streaming) return;
            touched.current = true;
            const pending = state.pending;

            setState((prev) => ({
                ...prev,
                pending: null,
                done: false,
                error: null,
                messages: [
                    ...prev.messages,
                    {
                        role: 'user',
                        content: text || `(${images.length} ${images.length === 1 ? 'image' : 'images'})`,
                        kind: 'text',
                        key: `user-${prev.messages.length}`,
                    },
                ],
            }));
            setStreaming(true);

            const controller = new AbortController();
            abortRef.current = controller;
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
                              history: state.messages
                                  .filter((m) => m.kind === 'text' && m.key !== 'welcome')
                                  .map((m) => ({ role: m.role, content: m.content })),
                              can_create_agents: true,
                              model: sessionModel || undefined,
                              images,
                              current_orchestration_id: currentOrchestrationId.current,
                              client_session_id: conversationId(),
                          }),
                      });

                if (!response.ok) throw new Error(`The builder could not be reached (${response.status}).`);
                await consume(response);
            } catch (error) {
                reportStreamEnd(error);
            } finally {
                setStreaming(false);
                abortRef.current = null;
                stopRequested.current = false;
            }
        },
        [consume, conversationId, reportStreamEnd, sessionModel, state.messages, state.pending, streaming],
    );

    /**
     * Stop the attached run.
     *
     * The POST is what ends it; nothing is aborted first, because cutting the
     * stream would drop the very event that says what happened. A run being
     * built is followed by an open stream, so the engine's own cancel arrives
     * on it and the reducer renders "Stopped." — with the timer below as the
     * backstop for a run wedged inside a provider call.
     *
     * A run parked on a question has **no** open stream: this product ends a
     * run's stream at the pause rather than holding it (which is why the
     * composer works here and had to be fixed in the commercial edition). So
     * nothing will report that cancel, and this says it itself.
     */
    const stop = useCallback(async () => {
        const parked = pendingRef.current;
        const runId = parked?.runId ?? runIdRef.current;
        if (!runId) return;

        const following = abortRef.current;
        stopRequested.current = true;
        setState((prev) => ({ ...prev, status: 'Stopping…' }));
        let ended = true;
        try {
            await cancelRun(runId);
        } catch {
            // Already finished, or unreachable. A run being followed still has
            // the timer below; a parked one is reported as it went.
            ended = false;
        }

        if (!following) {
            stopRequested.current = false;
            setState((prev) => ({
                ...prev,
                status: null,
                // The question stays open when the run did: it is still the
                // one thing this conversation can do.
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
            if (abortRef.current === following) following.abort();
        }, STOP_GRACE_MS);
    }, [cancelRun]);

    const saveBuilderModel = useCallback(async (model: string) => {
        const current = await (await fetch('/api/settings')).json();
        await fetch('/api/settings', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            // `agent_name` has no default in the settings model, so a payload
            // without it is refused — this endpoint is not a true patch.
            body: JSON.stringify({ agent_name: current.agent_name, builder_model: model }),
        });
        setBuilderModel(model);
    }, []);

    // `vision_capable` is a list of model names, so this resolves the same
    // three-step fallback the backend does: this conversation's pick, the
    // workspace builder model, then the workspace default behind it.
    const resolvedModel = sessionModel || builderModel || workspaceModel;
    const canAttach = Boolean(resolvedModel) && visionCapable.includes(resolvedModel);

    const value = useMemo(
        () => ({
            isOpen, entered, open, close, state, streaming, send, stop, newConversation,
            providers, canAttach, builderModel, sessionModel, setSessionModel, saveBuilderModel,
            setCurrentOrchestration,
        }),
        [isOpen, entered, open, close, state, streaming, send, stop, newConversation,
         providers, canAttach, builderModel, sessionModel, setSessionModel, saveBuilderModel,
         setCurrentOrchestration],
    );

    return <BuilderContext.Provider value={value}>{children}</BuilderContext.Provider>;
}
