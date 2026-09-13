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

export type BuilderMessageKind =
    | 'text'
    | 'banner'
    | 'step_progress'
    | 'human_prompt'
    | 'tool_call'
    | 'tool_result'
    | 'routing';

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
 * `resource_saved` is the one event whose whole point is that the panel does
 * not have to guess. Everything else is presentation.
 */
function bannerText(saved: SavedResource): string {
    const noun = saved.kind === 'orchestration' ? 'Orchestration' : saved.kind[0].toUpperCase() + saved.kind.slice(1);
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

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export function applyBuilderEvent(state: BuilderState, event: any, seq = 0): BuilderState {
    const type: string = event?.type ?? '';
    const key = `${type}-${event?.orch_step_id ?? ''}-${event?.tool_name ?? ''}-${event?.id ?? ''}-${seq}`;

    switch (type) {
        case 'orchestration_start':
            return { ...state, runId: event.run_id ?? state.runId, done: false, error: null };

        case 'step_start':
            return append(
                {
                    ...state,
                    messages: completeLastStep(state.messages),
                    status: event.step_name ?? null,
                    // Another step starting means the question was answered.
                    pending:
                        state.pending && state.pending.stepId !== (event.orch_step_id ?? null)
                            ? null
                            : state.pending,
                },
                { role: 'assistant', content: '', kind: 'step_progress', key,
                  stepName: event.step_name ?? '', stepStatus: 'running' },
            );

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
                toolName: event.tool_name ?? '', toolPreview: String(event.preview ?? ''),
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
        // the engine's router says where the run goes (`routing_decision`,
        // with the routing step's `step_type`). A bubble for both drew every
        // decision twice — "→ create → create". The router's is the one that
        // says where the run went; the run log still shows both, on purpose.
        //
        // The one case this drops: an evaluator whose label is not in its
        // `route_map` falls through to `next_step_id` and the router says
        // nothing, so no bubble — the run continuing is visible anyway.
        case 'evaluator_decision':
        case 'if_decision':
        case 'switch_decision':
            return state;

        case 'routing_decision':
            return append(state, {
                role: 'assistant', content: '', kind: 'routing', key,
                // Every branch must yield something truthy: BuilderPanel
                // renders a routing bubble only when `routingDecision` is, so
                // an empty string drops the line rather than showing it blank.
                // A switch that took its default has `decision: null` — that
                // is "default", not "nothing".
                routingDecision:
                    event.step_type === 'switch'
                        ? `case ${event.decision ?? 'default'}`
                        : String(event.decision ?? ''),
            });

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
                },
            );

        case 'orchestration_complete': {
            const summary = event.final_state?.summary;
            const withStatus = {
                ...state, messages: completeLastStep(state.messages), status: null, done: true, pending: null,
            };
            if (!summary) return withStatus;
            return append(withStatus, {
                role: 'assistant', content: String(summary), kind: 'text', key: `summary-${event.run_id ?? seq}`,
            });
        }

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
            return append(
                { ...state, messages: completeLastStep(state.messages), status: null,
                  done: true, error: String(event.error ?? 'Something went wrong.'), pending: null },
                { role: 'assistant', content: `**Something went wrong.** ${event.error ?? ''}`,
                  kind: 'text', key: `error-${seq}` },
            );

        // Everything else — per-step `final`, reasoning, task lists, parallel
        // bookkeeping — belongs to the run view, not to a build conversation.
        default:
            return state;
    }
}
