'use client';
/**
 * The AI builder, available from every screen.
 *
 * It used to be mounted inside the orchestration editor, which meant it could
 * only be reached from one screen and its conversation was destroyed by
 * navigating away. It lives in the app shell now, so the state is here.
 *
 * Each Synapse AI chat is its own (TABS2, F1061): `builderChats` keeps one store per chat id. The panel shows one of
 * them, a Synapse AI tab on the chat page another (`useBuilderChat`), and the same id in both is the same chat.
 */
import {
    createContext,
    useCallback,
    useContext,
    useEffect,
    useMemo,
    useRef,
    useState,
    useSyncExternalStore,
} from 'react';

import {
    type BuilderMessage,
    type BuilderState,
    type SavedResource,
} from '@/lib/builderEvents';
import { AGENT_BUILDER_SWITCH, asksForAgentBuilderSwitch, withoutBuildParam } from '@/components/assistant';
import { usePersisted } from '@/components/ui';
import type { OpenConversation } from '@/lib/builderReattach';
import type { ProviderModels } from '@/lib/models';
import { BuilderChats, newChatId, type ChatView, type SendOptions } from './builderChats';

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
 * What Synapse AI says before anything else, above the composer: what it answers
 * and what it builds (ASSIST1, D291; the build half is Prototype 2's words, M2c).
 */
export const BUILDER_GREETING = {
    title: 'What can I do for you?',
    text: "Ask about your workspace: what ran, what failed, what waits on you. Or describe a job in plain words, and I'll show you a plan to approve before I build anything.",
} as const;

/**
 * Things to ask for, as suggestions under the empty composer (M2c): a tap puts
 * one in the composer, unsent. They were bullets under the greeting.
 */
export const BUILDER_EXAMPLES = [
    'Summarise today',
    "Every weekday at 8, summarise yesterday's support tickets and post it to Slack",
    'Add a tool that files a ticket in our issue tracker',
    'Change the research workflow to ask me before it sends anything',
] as const;

const WELCOME: BuilderMessage = {
    role: 'assistant',
    kind: 'text',
    key: 'welcome',
    content: `${BUILDER_GREETING.title} ${BUILDER_GREETING.text}`,
};

interface OpenOptions {
    currentOrchestrationId?: string | null;
    /**
     * Words to put in the composer, unsent, for the person to read and send
     * (Home's "describe what you want to automate" and its suggestions, M2a).
     */
    prompt?: string;
    /** Words sent at once, as the person's message: Home's ask line and its chips (ASSIST1). */
    ask?: string;
    /** In a new chat, not the one the panel was showing (TABS2). */
    fresh?: boolean;
}

/** Words waiting for the composer; `n` tells one hand-over from the next with the same words. */
export type BuilderDraft = { text: string; n: number };

/** One chat as a view uses it: what it holds, and what it can be asked to do. */
export interface BuilderChatValue {
    /** The chat's id: the `client_session_id` its turns run under. */
    chatId: string;
    state: BuilderState;
    streaming: boolean;
    /** Read back, or needing no reading: until then a view shows neither greeting nor turns. */
    ready: boolean;
    /** `revising`: the words answer a waiting plan itself — its card's buttons, or typed after Change something
     * (F759). Without it, words typed while a plan waits are a new request, and the plan is set aside. `retryOf`:
     * a failed turn sent again on a stronger model (F1018). */
    send: (text: string, images?: string[], options?: SendOptions) => Promise<void>;
    /**
     * End the chat's run — the one being built, or the one parked on a
     * question. Both are the same POST; only who reports it back differs.
     */
    stop: () => Promise<void>;
}

interface BuilderContextValue extends BuilderChatValue {
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
    /** The words the last `open({ prompt })` handed over, for the panel's composer. */
    draft: BuilderDraft | null;
    /** The `n` of the last draft a composer took: one that mounts later (the panel opening) takes only a newer one. */
    draftTaken: number;
    takeDraft: (n: number) => void;
    close: () => void;
    /** The panel starts a new chat. The one it showed stays where it was, to be picked up again from a list. */
    newConversation: () => void;
    /** The panel shows chat `id`: one picked from its list, or a tab's (TABS2, F1062). */
    showChat: (id: string) => void;
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
    /** Every chat something shows, for `useBuilderChat`. */
    chats: BuilderChats;
    /** A turn's model and screen, read when it is sent, for `useBuilderChat`. */
    sendContext: () => { model: string; currentOrchestrationId: string | null };
}

const BuilderContext = createContext<BuilderContextValue | null>(null);

/** The panel's chat and the builder's app-wide controls. */
export function useBuilder(): BuilderContextValue {
    const ctx = useContext(BuilderContext);
    if (!ctx) throw new Error('useBuilder must be used inside <BuilderProvider>');
    return ctx;
}

/** Chat `id` as a view holds it while it shows it; `fresh` says there is nothing on the server to read back. */
function useHeldChat(chats: BuilderChats, id: string, fresh: boolean): ChatView & { chat: ReturnType<BuilderChats['get']> } {
    // Made on first sight, held from the effect: a view that never mounts holds nothing.
    // `fresh` counts only when the chat is made: a chat already held is the one it is.
    const chat = useMemo(() => chats.get(id, fresh), [chats, id, fresh]);
    useEffect(() => chats.hold(id, fresh), [chats, id, fresh]);
    const view = useSyncExternalStore(chat.subscribe, chat.getSnapshot, chat.getSnapshot);
    return { ...view, chat };
}

/**
 * One Synapse AI chat, for a view that shows a chat of its own: a Synapse AI tab (TABS2, F1061). The same id as the
 * panel's is the panel's chat, so the two draw one conversation.
 */
export function useBuilderChat(id: string, { fresh = false }: { fresh?: boolean } = {}): BuilderChatValue {
    const { chats, sendContext } = useBuilder();
    const { chat, state, streaming, ready } = useHeldChat(chats, id, fresh);
    const send = useCallback(
        (text: string, images: string[] = [], options: SendOptions = {}) => chat.send(text, images, options, sendContext()),
        [chat, sendContext],
    );
    return { chatId: id, state, streaming, ready, send, stop: chat.stop };
}

export function BuilderProvider({ children }: { children: React.ReactNode }) {
    const [isOpen, setIsOpen] = useState(false);
    const [entered, setEntered] = useState(false);
    const [draft, setDraft] = useState<BuilderDraft | null>(null);
    const [draftTaken, takeDraft] = useState(0);
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
    //: The pending unmount at the end of the close transition, so `open` can cancel it.
    const exitTimer = useRef<number | null>(null);

    const announce = useCallback((saved: SavedResource) => {
        window.dispatchEvent(new CustomEvent(RESOURCE_SAVED_EVENT, { detail: saved }));
    }, []);
    const [chats] = useState(() => new BuilderChats(WELCOME, announce));

    // The panel's chat. Minted once, not per render: a render that runs twice must not make two chats. It is held
    // for the life of the page, open or closed, so the panel's conversation survives navigating, as it always has.
    const [panelChat, setPanelChat] = useState<{ id: string; fresh: boolean }>(() => ({ id: newChatId(), fresh: true }));
    const panel = useHeldChat(chats, panelChat.id, panelChat.fresh);

    const modelRef = useRef(sessionModel);
    useEffect(() => { modelRef.current = sessionModel; }, [sessionModel]);
    const sendContext = useCallback(
        () => ({ model: modelRef.current, currentOrchestrationId: currentOrchestrationId.current }),
        [],
    );

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

    // Words `open({ ask })` sends once the panel is open (below `send`): held in a ref, the tick says there are some.
    const asked = useRef<string | null>(null);
    const [askTick, setAskTick] = useState(0);

    const newConversation = useCallback(() => {
        // A chat with nothing in it is already new. One with turns stays as it was — a question it waits on
        // included — for the panel's list and History to pick up again (TABS2); a day's wait still ends it (F214).
        if (panel.chat.isBlank()) return;
        setPanelChat({ id: newChatId(), fresh: true });
    }, [panel.chat]);

    const showChat = useCallback((id: string) => {
        setPanelChat((cur) => (cur.id === id ? cur : { id, fresh: false }));
    }, []);

    const open = useCallback((options?: OpenOptions) => {
        // An explicit answer still wins, for a caller that opens the panel about
        // something other than what it is showing. Everything else comes from
        // whichever screen has registered itself.
        if (options && 'currentOrchestrationId' in options) {
            currentOrchestrationId.current = options.currentOrchestrationId ?? null;
        }
        if (options?.fresh) newConversation();
        if (options?.prompt) {
            const text = options.prompt;
            setDraft((cur) => ({ text, n: (cur?.n ?? 0) + 1 }));
        }
        if (options?.ask?.trim()) { asked.current = options.ask.trim(); setAskTick((n) => n + 1); }
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
    }, [newConversation]);

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

    // `?build=agent-builder` (SW1): someone leaving OpenAI Agent Builder,
    // sent from the site. The panel opens with the lead line in the composer
    // for their export to go under; the parameter goes, so a reload does not
    // open it again. Scheduled, so the opening is not a render inside this
    // effect — and the parameter goes in the same callback, so a cancelled
    // first pass (Strict Mode runs effects twice) leaves it for the second.
    // `null`, not the entry's own state: Next's router only follows a
    // `replaceState` that does not carry its marker, and one that missed this
    // put the parameter back on the next navigation it made.
    useEffect(() => {
        if (!asksForAgentBuilderSwitch(window.location.search)) return;
        const timer = window.setTimeout(() => {
            window.history.replaceState(null, '', withoutBuildParam(window.location.href));
            open({ prompt: AGENT_BUILDER_SWITCH.lead });
        }, 0);
        return () => window.clearTimeout(timer);
    }, [open]);

    // A reload brings back the panel's conversation still open (F214): the panel shows that chat, redrawn from its
    // run rows, and a run still working is followed. Only onto a panel nobody has typed into yet.
    const firstChat = useRef(panel.chat);
    useEffect(() => {
        let cancelled = false;
        (async () => {
            try {
                const response = await fetch('/api/builder/conversation');
                if (!response.ok || cancelled) return;
                const { conversation } = (await response.json()) as { conversation: OpenConversation | null };
                if (!conversation || cancelled || !firstChat.current.isBlank()) return;
                const id = conversation.client_session_id;
                chats.get(id, true).adopt(conversation);
                setPanelChat((cur) => (cur.id === firstChat.current.id ? { id, fresh: true } : cur));
            } catch {
                /* the panel opens on its greeting, as before */
            }
        })();
        return () => { cancelled = true; };
    }, [chats]);

    const send = useCallback(
        (text: string, images: string[] = [], options: SendOptions = {}) => panel.chat.send(text, images, options, sendContext()),
        [panel.chat, sendContext],
    );

    // Home's ask line (ASSIST1): the words go as the person's message once the panel is open, and wait while a
    // turn is still answering, then go.
    useEffect(() => {
        const text = asked.current;
        if (!text || !isOpen || panel.streaming) return;
        asked.current = null;
        void send(text);
    }, [askTick, isOpen, panel.streaming, send]);

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

    const { state, streaming, ready } = panel;
    const stop = panel.chat.stop;
    const value = useMemo(
        () => ({
            chatId: panelChat.id, state, streaming, ready, send, stop,
            isOpen, entered, open, draft, draftTaken, takeDraft, close, newConversation, showChat,
            providers, canAttach, builderModel, sessionModel, setSessionModel, saveBuilderModel,
            setCurrentOrchestration, chats, sendContext,
        }),
        [panelChat.id, state, streaming, ready, send, stop, isOpen, entered, open, draft, draftTaken, close,
         newConversation, showChat, providers, canAttach, builderModel, sessionModel, setSessionModel,
         saveBuilderModel, setCurrentOrchestration, chats, sendContext],
    );

    return <BuilderContext.Provider value={value}>{children}</BuilderContext.Provider>;
}
