/**
 * Engine events → the AI builder panel's messages.
 *
 * A pure reducer, deliberately: both products drive the same builder
 * orchestration but reach it differently — open source streams the run
 * directly from `/api/builder/chat`, the commercial edition queues it on the
 * worker fleet and reads `/runs/{id}/stream`. The events are identical, so the
 * mapping is written once and synced.
 *
 * It is also idempotent over a replay. A reconnecting stream starts from
 * `Last-Event-ID: 0` and re-delivers everything, so applying the same event
 * twice must not append the message twice.
 */

import { reasonHeadline } from '@/lib/runErrors';

export type BuilderMessageKind =
    | 'text'
    | 'banner'
    | 'step_progress'
    | 'human_prompt'
    | 'tool_call'
    | 'tool_result'
    | 'routing'
    | 'connect'
    | 'findings'
    | 'action'
    | 'try_stronger'
    | 'open_chat';

/** A failed turn's one offer (F1018): the same message again, once, on a stronger model the server picked. */
export interface TryStronger {
    model: string;
    name: string;
    price: string;
    /** "Try again on <name> (about $X per 1M output tokens)": the button's words, from the server. */
    label: string;
    /** The failed run the press tries again. */
    retry_of: string;
}

/**
 * A Connect card the builder put up (BWA1, `prepare_connection`): what to
 * connect, and where the person's key or sign-in goes. Never a secret; the
 * card sends what the person types straight to the host's own routes.
 */
export interface ConnectSpec {
    id: string;
    kind: 'app' | 'mcp_server' | 'provider_key' | 'tool_secret' | 'settings';
    title: string;
    why?: string;
    app?: { slug: string; name?: string; auth?: 'oauth' | 'bearer' | 'none' | string; url?: string; token_hint?: string };
    server?: { name: string; label?: string; url: string };
    provider?: { id: string; label?: string };
    /**
     * A tool's key. For the tools a build made from a read API (F945): every one of the provider's (`tools`), the
     * scheme its key goes in with, and each account part of its host the person fills in (Mailchimp's `dc`).
     */
    tool?: {
        id: string; name: string; header: string; scheme?: string; tools?: string[];
        host_params?: { name: string; says: string }[];
    };
    screen?: { id: string; label?: string };
    /** A build waits on this settings screen (F1104): the card's press says "Connected <this>.", and the build checks. */
    waits_for?: string;
}

/**
 * What the builder's Researcher found for a goal the workspace has no tool for
 * (BWA1, F577, `report_findings`): one option per provider, how it would
 * connect here, what the person needs, and the pages it rests on.
 */
export interface ResearchFindings {
    goal?: string;
    options: {
        provider: string;
        how: 'catalog_app' | 'mcp' | 'api' | 'partner' | 'none' | 'unclear';
        detail?: string;
        /** `partner`: the words that say only approved partners get in (F1094). */
        partner_words?: string[];
        needs?: string;
        sources?: string[];
    }[];
}

/**
 * An action card the assistant put up (ASSIST1, `prepare_action`): what it
 * would do, and a button per choice. Nothing has been done: each button
 * carries a token for that one act by this person, which the host's act
 * route redeems when it is pressed. A run's question carries the question
 * instead, answered through the run's own door.
 */
export interface ActionButton {
    label: string;
    tool: string;
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    arguments: Record<string, any>;
    token: string;
    expires_in_seconds?: number;
    /** The assistant turn that put the card up: a press is kept on that turn's card, for the next turn (SAI2-P4). */
    run_id?: string | null;
}

export interface ActionSpec {
    id: string;
    title: string;
    detail?: string;
    /** Stops or undoes something: said in the danger colour. */
    danger?: boolean;
    buttons: ActionButton[];
    question?: { run_id: string; prompt?: string; step_id?: string | null };
}

/** What the builder changed, as the dispatcher reported it. */
export interface SavedResource {
    kind: 'agent' | 'orchestration' | 'tool' | 'skill' | 'schedule';
    action: 'created' | 'updated' | 'deleted';
    id: string | null;
    name: string | null;
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    resource?: any;
}

export interface BuilderMessage {
    role: 'user' | 'assistant';
    content: string;
    kind: BuilderMessageKind;
    /** Stable within one conversation, so a replay replaces rather than appends. */
    key: string;
    stepName?: string;
    stepStatus?: 'running' | 'complete';
    toolName?: string;
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    toolArgs?: Record<string, any>;
    toolPreview?: string;
    routingDecision?: string;
    saved?: SavedResource;
    /** A `connect` message's card. */
    connect?: ConnectSpec;
    /**
     * A `human_prompt`'s step: which of the builder's questions it is (its plan,
     * a change, or a clarifying question), so the panel can draw a plan as a
     * card. `contextKey` is the live event's own word for the same thing.
     */
    stepId?: string | null;
    contextKey?: string | null;
    /**
     * A `human_prompt`'s typed object, when its step's context is one that names
     * itself (`{object: 'change', …}`: Synapse AI's checked change, F795). The
     * card draws from it; the prompt's words stay the question for everything
     * else, and for runs started before the object was sent.
     */
    context?: Record<string, unknown> | null;
    /** A findings card (`kind: 'findings'`). */
    findings?: ResearchFindings;
    /** An action card (`kind: 'action'`). */
    action?: ActionSpec;
    /** A failed turn's offer (`kind: 'try_stronger'`, F1018). */
    tryStronger?: TryStronger;
    /** "Open that chat" (`kind: 'open_chat'`): the person's own Synapse AI chat holding a workflow (TABS2, F1063). */
    chat?: { id: string; title: string };
}

export interface BuilderState {
    messages: BuilderMessage[];
    /**
     * Set while a human step is open; the panel answers with these.
     *
     * Cleared by the run moving on — a later step starting, or the run ending
     * — and not only by the panel sending an answer. A re-attaching stream
     * replays from the first event, and a question cleared only locally came
     * back open over a run that had long since resumed.
     */
    pending: { runId: string; field: string; stepId: string | null } | null;
    /** The line under the composer while the run is working. */
    status: string | null;
    runId: string | null;
    done: boolean;
    error: string | null;
}

export function emptyBuilderState(messages: BuilderMessage[] = []): BuilderState {
    return { messages, pending: null, status: null, runId: null, done: false, error: null };
}

/**
 * Whether the last thing said is the door's refusal of an answer (BWA1): the
 * run is still parked on its question, so the panel gives the question back
 * and the next message answers it again. A host clears `pending` as it sends
 * an answer, so only it can restore what it cleared.
 */
export function refusedAnswer(state: BuilderState): boolean {
    const last = state.messages[state.messages.length - 1];
    return !state.done && !!last && /^notice-(card|password)-/.test(last.key);
}

/**
 * `resource_saved` is the one event whose whole point is that the panel does
 * not have to guess. Everything else is presentation.
 */
function bannerText(saved: SavedResource): string {
    const noun = saved.kind === 'orchestration' ? 'Workflow' : saved.kind[0].toUpperCase() + saved.kind.slice(1);
    const count = saved.resource?.count as number | undefined;
    if (saved.kind === 'tool' && typeof count === 'number') {
        return `Imported ${count} tool${count === 1 ? '' : 's'} into ${saved.name}`;
    }
    const verb = saved.action === 'created' ? 'created' : saved.action === 'updated' ? 'updated' : 'deleted';
    return `${noun} “${saved.name ?? saved.id ?? 'untitled'}” ${verb}`;
}

function append(state: BuilderState, message: BuilderMessage): BuilderState {
    // Replay safety: the same event carries the same key, so re-applying it
    // overwrites its own message instead of adding a second copy.
    const at = state.messages.findIndex((m) => m.key === message.key);
    const messages =
        at >= 0
            ? state.messages.map((m, i) => (i === at ? message : m))
            : [...state.messages, message];
    return { ...state, messages };
}

/** Close the newest still-running step marker. */
function completeLastStep(messages: BuilderMessage[]): BuilderMessage[] {
    for (let i = messages.length - 1; i >= 0; i--) {
        if (messages[i].kind === 'step_progress' && messages[i].stepStatus === 'running') {
            const next = [...messages];
            next[i] = { ...next[i], stepStatus: 'complete' };
            return next;
        }
    }
    return messages;
}

/**
 * The human steps that are a plan's review (F759): each feeds a gate that approves in code (F754). While one waits,
 * it is answered through its card (Go ahead, Change something, Stop instead) or by a typed yes; anything else typed
 * is a new request, and the plan is set aside. A test in open source derives this list from the definition.
 */
export const PLAN_REVIEW_STEPS: readonly string[] = ['step_human_review', 'step_manage_review'];

/** What those gates take as a yes, word for word (checked against the definition in open source's tests). */
export const APPROVAL_WORDS: readonly string[] = [
    'approve', 'approved', 'ok', 'okay', 'yes', 'looks good', 'lgtm', 'go ahead', 'build it', 'proceed',
    'sounds good', 'do it', '👍',
];

/** Whether `text` is a yes the gates take: trimmed, lowered and stripped of spaces, dots and bangs at its ends. */
export function isApproval(text: string): boolean {
    const said = text.trim().toLowerCase().replace(/^[ .!]+|[ .!]+$/g, '');
    return APPROVAL_WORDS.includes(said);
}

/** What the conversation says when a waiting plan is set aside for a new request (F759). */
export const SET_ASIDE_NOTE = 'The plan above was set aside: nothing in it was done.';

type Waiting = { runId: string; field: string; stepId: string | null };

/** Whether what waits is a plan's review, not a question. */
export function isPlanReview(pending: Waiting | null): boolean {
    return !!pending?.stepId && PLAN_REVIEW_STEPS.includes(pending.stepId);
}

/**
 * What a message typed while the builder waits does (F759). A question takes whatever is typed. A plan takes its
 * yes, or what the person types while changing it (its card's Change something); anything else sets it aside and
 * starts a new request: ASSIST1's dev pass typed three unrelated requests at a waiting plan, and each was folded
 * into the plan while the question went unanswered.
 */
export function typedWhilePending(pending: Waiting, text: string, revising: boolean): 'answer' | 'set_aside' {
    if (!isPlanReview(pending) || revising || isApproval(text)) return 'answer';
    return 'set_aside';
}

/** A JSON string's own words, from the text between its quotes; null when they cannot be read. */
function unquoted(body: string): string | null {
    try {
        return JSON.parse(`"${body}"`) as string;
    } catch {
        return null;
    }
}

/**
 * What the step trail says for a tool's result (F761). A card's `prepare_action` answers JSON for the model
 * (`{"card": …, "say": …}`), and the trail showed it raw, escapes and all; it shows what the card says, or a
 * refusal's sentence. The engine cuts a preview at 500 characters, so the `say` or `error` is also read from a
 * preview that no longer parses. Any other result is left as it was.
 */
export function previewInWords(preview: string): string {
    const text = preview.trim();
    if (!text.startsWith('{')) return preview;
    try {
        const parsed = JSON.parse(text) as Record<string, unknown>;
        for (const key of ['say', 'error'] as const) {
            if (typeof parsed[key] === 'string' && parsed[key]) return parsed[key] as string;
        }
        return preview;
    } catch {
        for (const key of ['say', 'error']) {
            const found = new RegExp(`"${key}"\\s*:\\s*"((?:[^"\\\\]|\\\\.)*)"`).exec(text);
            const words = found ? unquoted(found[1]) : null;
            if (words) return words;
        }
        return preview;
    }
}

/**
 * A tool result's words for the trail, or '' when it is only data (F933): the trail drew
 * "↳ [{"name": "time", …" and `{"_not_found": […]}` as code. A card's `say` or a refusal's
 * `error` is read out of its JSON first (`previewInWords`); the chip still names the tool.
 */
function trailPreview(preview: string): string {
    const words = previewInWords(preview);
    return /^\s*[[{]/.test(words) ? '' : words;
}

/** The steps that only choose a branch: the panel shows what the builder did, never how it branched (F933). */
const ROUTING_STEP_TYPES = new Set(['evaluator', 'if_else', 'switch']);

/**
 * Whether the trail draws `message` (F899, F933). A running step is the working line under
 * the conversation, not a row of its own, so one loader moves; it joins the trail once done.
 * A route (an older conversation's) and a result that is only data draw nothing.
 */
export function shownInTrail(message: BuilderMessage): boolean {
    if (message.kind === 'step_progress') return message.stepStatus === 'complete';
    if (message.kind === 'routing') return false;
    if (message.kind === 'tool_result') return !!message.toolPreview;
    return true;
}

/** Where the JSON value that opens `text` ends (its closing bracket's index + 1), or -1 when none closes. */
function jsonEnd(text: string): number {
    let depth = 0;
    let inString = false;
    for (let i = 0; i < text.length; i++) {
        const c = text[i];
        if (inString) {
            if (c === '\\') i++;
            else if (c === '"') inString = false;
        } else if (c === '"') inString = true;
        else if (c === '{' || c === '[') depth++;
        else if (c === '}' || c === ']') {
            depth--;
            if (depth === 0) return i + 1;
        }
    }
    return -1;
}

/**
 * A reply with the JSON it opened with cut (SAI2's rendering row: a stray `{}` opened a gpt-4.1-mini reporter
 * reply). Only a whole JSON object at the very start, or an array of objects, with words after it, goes. A reply
 * that is only JSON, or that opens with a bracket that is words (`[ ] Connect…`, `[1] …`, a markdown link), is
 * left as it was (SAI2's review, S3).
 */
export function withoutOpeningJson(reply: string): string {
    const text = reply.trimStart();
    if (!text.startsWith('{') && !text.startsWith('[')) return reply;
    const end = jsonEnd(text);
    const rest = end > 0 ? text.slice(end).trim() : '';
    if (!rest) return reply;
    let opened: unknown;
    try {
        opened = JSON.parse(text.slice(0, end));
    } catch {
        return reply;
    }
    const isObject = (v: unknown) => typeof v === 'object' && v !== null && !Array.isArray(v);
    const data = isObject(opened) || (Array.isArray(opened) && opened.length > 0 && isObject(opened[0]));
    return data ? rest : reply;
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export function applyBuilderEvent(state: BuilderState, event: any, seq = 0): BuilderState {
    const type: string = event?.type ?? '';
    const key = `${type}-${event?.orch_step_id ?? ''}-${event?.tool_name ?? ''}-${event?.id ?? ''}-${seq}`;

    switch (type) {
        case 'orchestration_start':
            return { ...state, runId: event.run_id ?? state.runId, done: false, error: null };

        case 'step_start': {
            const moved: BuilderState = {
                ...state,
                messages: completeLastStep(state.messages),
                // Another step starting means the question was answered.
                pending:
                    state.pending && state.pending.stepId !== (event.orch_step_id ?? null)
                        ? null
                        : state.pending,
            };
            // A routing step ("Route: What Was Asked") is neither a line nor the working line (F933).
            if (ROUTING_STEP_TYPES.has(event.step_type)) return moved;
            return append(
                { ...moved, status: event.step_name ?? null },
                { role: 'assistant', content: '', kind: 'step_progress', key,
                  stepName: event.step_name ?? '', stepStatus: 'running' },
            );
        }

        case 'step_complete':
            return { ...state, messages: completeLastStep(state.messages) };

        case 'thinking':
        case 'status':
            return { ...state, status: event.message ?? state.status };

        case 'tool_execution':
            return append(state, {
                role: 'assistant', content: '', kind: 'tool_call', key,
                toolName: event.tool_name ?? '', toolArgs: event.args ?? {},
            });

        case 'tool_result':
            return append(state, {
                role: 'assistant', content: '', kind: 'tool_result', key,
                toolName: event.tool_name ?? '', toolPreview: trailPreview(String(event.preview ?? '')),
            });

        case 'resource_saved': {
            const saved: SavedResource = {
                kind: event.kind, action: event.action, id: event.id ?? null,
                name: event.name ?? null, resource: event.resource,
            };
            return append(state, {
                role: 'assistant', content: bannerText(saved), kind: 'banner',
                key: `saved-${saved.kind}-${saved.id}-${saved.action}`, saved,
            });
        }

        // Each routing step speaks twice: its executor announces its own
        // result (`evaluator_decision`, `if_decision`, `switch_decision`), then
        // the engine's router says where the run goes (`routing_decision`).
        // The panel drew the router's as "→ case create" and "→ true": the
        // builder's branch decisions, which are its own business (F933). The
        // panel shows what the builder did; the run log keeps every decision.
        case 'evaluator_decision':
        case 'if_decision':
        case 'switch_decision':
        case 'routing_decision':
            return state;

        case 'human_input_required':
            return append(
                {
                    ...state,
                    status: null,
                    runId: event.run_id ?? state.runId,
                    pending: {
                        runId: event.run_id,
                        field: event.fields?.[0]?.name ?? 'answer',
                        stepId: event.orch_step_id ?? null,
                    },
                },
                {
                    role: 'assistant', content: String(event.prompt ?? ''), kind: 'human_prompt',
                    key: `human-${event.orch_step_id ?? ''}-${seq}`,
                    stepId: event.orch_step_id ?? null,
                    contextKey: event.context_key ?? null,
                    ...(event.context && typeof event.context === 'object' ? { context: event.context } : {}),
                },
            );

        case 'orchestration_complete': {
            const summary = event.final_state?.summary;
            const withStatus = {
                ...state, messages: completeLastStep(state.messages), status: null, done: true, pending: null,
            };
            if (!summary) return withStatus;
            const said = append(withStatus, {
                role: 'assistant', content: withoutOpeningJson(String(summary)), kind: 'text',
                key: `summary-${event.run_id ?? seq}`,
            });
            // "Open that chat" came during the turn (F1063); it goes under the answer that says why.
            const asked = said.messages.map((m) => m.role).lastIndexOf('user');
            const ways = said.messages.filter((m, i) => i > asked && m.kind === 'open_chat');
            return ways.length ? { ...said, messages: [...said.messages.filter((m) => !ways.includes(m)), ...ways] } : said;
        }

        // A Connect card (BWA1): where a key or a sign-in goes instead of the chat.
        // Keyed on the card's own id, so a replay redraws it rather than adding one.
        case 'connection_needed':
            return append(state, {
                role: 'assistant', content: String(event.title ?? 'Connect'), kind: 'connect',
                key: `connect-${event.id ?? seq}`, connect: event as ConnectSpec,
            });

        // An action card (ASSIST1): what the assistant would do, for the person
        // to press. Keyed on the card's own id, so a replay redraws it.
        case 'action_needed': {
            if (!event.id || !Array.isArray(event.buttons)) return state;
            // Each button carries the turn that showed it, so its press is kept on that turn's card (SAI2-P4).
            const runId = event.run_id ?? state.runId;
            const action = { ...event, buttons: event.buttons.map((b: ActionButton) => ({ ...b, run_id: runId })) };
            return append(state, {
                role: 'assistant', content: String(event.title ?? 'Action'), kind: 'action',
                key: `action-${event.id}`, action: action as ActionSpec,
            });
        }

        // What the Researcher found (BWA1, F577): a card of the options, each with
        // its sources. One per research step; a replay redraws it.
        case 'research_findings':
            if (!Array.isArray(event.options) || event.options.length === 0) return state;
            return append(state, {
                role: 'assistant', content: String(event.goal ?? 'Who can do it'), kind: 'findings',
                key: `findings-${event.orch_step_id ?? ''}-${seq}`,
                findings: { goal: event.goal, options: event.options } as ResearchFindings,
            });

        // The person's own Synapse AI chat holds the workflow this one was asked to change (TABS2, F1063): the reply
        // says so, and this is the way there. Keyed on the chat, so a replay redraws it rather than adding one.
        case 'open_chat':
            if (typeof event.chat_id !== 'string' || !event.chat_id) return state;
            return append(state, {
                role: 'assistant', content: String(event.title ?? ''), kind: 'open_chat',
                key: `open-chat-${event.chat_id}`, chat: { id: event.chat_id, title: String(event.title ?? '') },
            });

        // The builder's door (BWA1, D274): a message or an answer it would not keep
        // (a card number, a password), or a key it took out of one. Said as the
        // builder's reply. A refused message ends the turn; a refused answer
        // leaves its question open, so the person answers it again.
        case 'builder_notice': {
            const final = event.final === true;
            // `kept` is the person's message as it was kept (a key masked, a
            // refused message not kept at all). It replaces what they typed, so
            // the screen matches the conversation and the next turn's history
            // never sends the secret back.
            const kept = typeof event.kept === 'string' && event.kept ? event.kept : null;
            const at = kept === null ? -1 : state.messages.map((m) => m.role).lastIndexOf('user');
            const shown = at < 0 ? state : {
                ...state,
                messages: state.messages.map((m, i) => (i === at ? { ...m, content: kept as string } : m)),
            };
            return append(
                final ? { ...shown, status: null, done: true, pending: null } : shown,
                {
                    role: 'assistant', content: String(event.message ?? ''), kind: 'text',
                    key: `notice-${event.reason ?? ''}-${seq}`,
                },
            );
        }

        // A failed turn's one offer (F1018): drawn under the failure, keyed on the run it tries again.
        case 'try_stronger':
            if (typeof event.retry_of !== 'string' || typeof event.label !== 'string') return state;
            return append(state, {
                role: 'assistant', content: event.label, kind: 'try_stronger', key: `try-${event.retry_of}`,
                tryStronger: event as TryStronger,
            });

        case 'orchestration_error':
        case 'step_error':
            // A requested cancel arrives as this event too — the engine's own
            // `_finish_cancelled`, whether the panel's Stop or the Runs page
            // asked. It is not a failure, and saying "Something went wrong"
            // blames the product for doing what it was told.
            if (type === 'orchestration_error' && event.error === 'Cancelled') {
                return append(
                    { ...state, messages: completeLastStep(state.messages), status: null,
                      done: true, error: null, pending: null },
                    { role: 'assistant', content: 'Stopped.', kind: 'text', key: `stopped-${seq}` },
                );
            }
            {
                // A step's own words (`reason`, the engine's F195), as the run page shows them: `error` is the
                // sanitised sentence, so a provider's refusal for the key's usage limit read as an internal error
                // (F931). A traceback says its last line; an older engine's event has only `error`.
                const said = typeof event.reason === 'string' && event.reason
                    ? reasonHeadline(event.reason) : event.error;
                return append(
                    { ...state, messages: completeLastStep(state.messages), status: null,
                      done: true, error: String(said ?? 'Something went wrong.'), pending: null },
                    { role: 'assistant', content: `**Something went wrong.** ${said ?? ''}`,
                      kind: 'text', key: `error-${seq}` },
                );
            }

        // Everything else — per-step `final`, reasoning, task lists, parallel
        // bookkeeping — belongs to the run view, not to a build conversation.
        default:
            return state;
    }
}
