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

const WELCOME: BuilderMessage = {
    role: 'assistant',
    kind: 'text',
    key: 'welcome',
    content:
        "I build things in this workspace — agents, orchestrations, tools, skills and schedules. " +
        "Tell me what you want to happen and I'll ask what I need, show you a plan, and build it once you approve.\n\n" +
        // Not italicised. Three italic lines is most of what made this read as
        // filler rather than as examples you could type.
        '- "Every weekday at 8, summarise yesterday\'s support tickets and post it to Slack"\n' +
        '- "Add a tool that files a ticket in our issue tracker"\n' +
        '- "Change the research workflow to ask me before it sends anything"',
};

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
     * True once the panel should be in its open position — one frame after
     * `isOpen`, and false for the length of the exit while `isOpen` is still
     * true. The panel reads this to transition; nothing else needs it.
     */
    entered: boolean;
    open: (options?: OpenOptions) => void;
    close: () => void;
    state: BuilderState;
    streaming: boolean;
    send: (text: string) => Promise<void>;
    newConversation: () => void;
    providers: Record<string, ProviderModels>;
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
                }
                if (settingsRes.ok) {
                    const data = await settingsRes.json();
                    setBuilderModel(data.builder_model || '');
                }
            } catch {
                /* the picker falls back to the workspace default */
            }
        })();
        return () => { cancelled = true; };
    }, []);

    const open = useCallback((options?: OpenOptions) => {
        if (options && 'currentOrchestrationId' in options) {
            currentOrchestrationId.current = options.currentOrchestrationId ?? null;
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
        window.setTimeout(() => setIsOpen(false), EXIT_MS);
    }, []);

    const newConversation = useCallback(() => {
        abortRef.current?.abort();
        clientSessionId.current = newSessionId();
        setState(emptyBuilderState([WELCOME]));
        setStreaming(false);
    }, []);

    const announce = useCallback((saved: SavedResource) => {
        window.dispatchEvent(new CustomEvent(RESOURCE_SAVED_EVENT, { detail: saved }));
    }, []);

    const consume = useCallback(
        async (response: Response) => {
            const reader = response.body?.getReader();
            if (!reader) return;
            const decoder = new TextDecoder();
            let buffer = '';
            let seq = 0;

            for (;;) {
                const { done, value } = await readWithStallTimeout(reader, abortRef.current!);
                if (done) break;
                buffer += decoder.decode(value, { stream: true });
                const lines = buffer.split('\n');
                buffer = lines.pop() ?? '';
                for (const line of lines) {
                    if (!line.startsWith('data: ')) continue;
                    let event: unknown;
                    try {
                        event = JSON.parse(line.slice(6));
                    } catch {
                        continue; // a heartbeat or a half-written frame
                    }
                    seq += 1;
                    // eslint-disable-next-line @typescript-eslint/no-explicit-any
                    const parsed = event as any;
                    setState((prev) => applyBuilderEvent(prev, parsed, seq));
                    if (parsed?.type === 'resource_saved') announce(parsed as SavedResource);
                }
            }
        },
        [announce],
    );

    const send = useCallback(
        async (text: string) => {
            if (!text.trim() || streaming) return;
            const pending = state.pending;

            setState((prev) => ({
                ...prev,
                pending: null,
                done: false,
                error: null,
                messages: [
                    ...prev.messages,
                    { role: 'user', content: text, kind: 'text', key: `user-${prev.messages.length}` },
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
                              current_orchestration_id: currentOrchestrationId.current,
                              client_session_id: conversationId(),
                          }),
                      });

                if (!response.ok) throw new Error(`The builder could not be reached (${response.status}).`);
                await consume(response);
            } catch (error) {
                if ((error as Error)?.name !== 'AbortError') {
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
            } finally {
                setStreaming(false);
                abortRef.current = null;
            }
        },
        [consume, conversationId, sessionModel, state.messages, state.pending, streaming],
    );

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

    const value = useMemo(
        () => ({
            isOpen, entered, open, close, state, streaming, send, newConversation,
            providers, builderModel, sessionModel, setSessionModel, saveBuilderModel,
        }),
        [isOpen, entered, open, close, state, streaming, send, newConversation,
         providers, builderModel, sessionModel, setSessionModel, saveBuilderModel],
    );

    return <BuilderContext.Provider value={value}>{children}</BuilderContext.Provider>;
}
