/*
 * A helper's lane, from its events (SUB1a, S7).
 *
 * An agent that can send helpers (`spawn_subtask`) streams each one between a
 * `helper_started` and a `helper_finished` that carries its record; what it does
 * in between comes wrapped, `helper_event` {helper_id, helper_name, event}, so
 * nothing that reads the agent's own events mistakes a helper's for its own. A
 * background helper's events are its own conversation's (`helperc_…`), read the
 * same way. A saved turn keeps each record under `helpers`.
 *
 * Pure: no React, no fetch — each product folds its stream through
 * `foldHelpers` and draws `HelperLane`.
 */

export type HelperStatus = 'running' | 'completed' | 'failed' | 'cancelled' | 'timed_out';

export type HelperCall = {
    tool: string;
    args?: Record<string, unknown> | null;
    result?: string;
    refused?: boolean;
    reason?: string;
};

export type HelperLaneData = {
    id: string;
    name: string;
    /** Its brief: what it was asked to do (its instructions, if it is saved as an agent). */
    task: string;
    tools: string[];
    withheld?: string[];
    model?: string;
    /** `background`: it works on after its agent's turn, and posts its result when it is done. */
    mode: 'wait' | 'background';
    status: HelperStatus;
    seconds?: number;
    calls: HelperCall[];
    /** Its answer, once it has one. */
    answer: string;
    /** Its answer as it is being written. */
    streaming?: string;
    /** What it is doing now, in a few words. */
    activity?: string;
    error?: string;
    /** A background helper's own conversation, whose events its lane reads. */
    sessionId?: string;
};

/** An event or a record as the wire has it: read field by field below. */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Wire = Record<string, any>;

/** A lane from a saved record (a reopened turn, or `helper_finished`). */
export function laneFromRecord(record: Wire): HelperLaneData {
    return {
        id: String(record.id ?? ''),
        name: String(record.name ?? 'Helper'),
        task: String(record.task ?? ''),
        tools: Array.isArray(record.tools) ? record.tools.map(String) : [],
        withheld: Array.isArray(record.withheld) ? record.withheld.map(String) : [],
        model: record.model ? String(record.model) : undefined,
        mode: record.mode === 'background' ? 'background' : 'wait',
        status: (record.status as HelperStatus) || 'completed',
        seconds: typeof record.seconds === 'number' ? record.seconds : undefined,
        calls: Array.isArray(record.calls) ? record.calls.map((c: Wire) => ({
            tool: String(c.tool ?? ''), args: c.args ?? null, result: c.result ?? '',
            ...(c.refused ? { refused: true, reason: c.reason ?? '' } : {}),
        })) : [],
        answer: String(record.answer ?? ''),
        error: record.error ? String(record.error) : undefined,
        sessionId: record.session_id ? String(record.session_id) : undefined,
    };
}

/** Whether an event is a helper's (and so for `foldHelpers`, never the agent's own). */
export function isHelperEvent(event: Wire): boolean {
    return event?.type === 'helper_started' || event?.type === 'helper_event' || event?.type === 'helper_finished';
}

function update(lanes: HelperLaneData[], id: string, change: (lane: HelperLaneData) => HelperLaneData): HelperLaneData[] {
    return lanes.map((lane) => (lane.id === id ? change(lane) : lane));
}

/** The lanes after one event. Events of a helper not seen starting are ignored. */
export function foldHelpers(lanes: HelperLaneData[], event: Wire): HelperLaneData[] {
    if (event?.type === 'helper_started') {
        if (lanes.some((lane) => lane.id === event.helper_id)) return lanes;
        return [...lanes, {
            id: String(event.helper_id), name: String(event.helper_name || 'Helper'), task: String(event.task || ''),
            tools: Array.isArray(event.tools) ? event.tools.map(String) : [],
            withheld: Array.isArray(event.withheld) ? event.withheld.map(String) : [],
            model: event.model ? String(event.model) : undefined,
            mode: event.mode === 'background' ? 'background' : 'wait',
            status: 'running', calls: [], answer: '', streaming: '',
            sessionId: event.session_id ? String(event.session_id) : undefined,
        }];
    }
    if (event?.type === 'helper_finished') {
        const record = event.record ? laneFromRecord(event.record) : null;
        return update(lanes, String(event.helper_id), (lane) => ({
            ...lane, ...(record ?? {}), status: (event.status as HelperStatus) || record?.status || lane.status,
            sessionId: lane.sessionId ?? record?.sessionId, streaming: '', activity: undefined,
        }));
    }
    if (event?.type !== 'helper_event') return lanes;
    return update(lanes, String(event.helper_id), (lane) => foldLaneEvent(lane, event.event || {}));
}

/** What a lane keeps of the work it shows: a helper's, or a team member's (TEAM1b). */
export type LaneWork = { calls: HelperCall[]; streaming?: string; activity?: string };

/**
 * A lane after one of its agent's own events, unwrapped: a call, its result, its answer as it is written, what it
 * is thinking. A helper's lane and a team member's fold their work the same way.
 */
export function foldLaneEvent<L extends LaneWork>(lane: L, inner: Wire): L {
    switch (inner.type) {
        case 'tool_execution':
            return { ...lane, calls: [...lane.calls, { tool: String(inner.tool_name || ''), args: inner.args ?? null, result: '' }],
                     activity: undefined };
        case 'tool_result': {
            const calls = [...lane.calls];
            const at = calls.findLastIndex((c) => c.tool === inner.tool_name && !c.result && !c.refused);
            if (at !== -1) {
                calls[at] = { ...calls[at], result: String(inner.preview ?? ''),
                              ...(inner.refused ? { refused: true, reason: String(inner.reason ?? '') } : {}) };
            }
            return { ...lane, calls };
        }
        case 'answer_delta':
            return { ...lane, streaming: `${lane.streaming ?? ''}${inner.text ?? ''}` };
        case 'answer_reset':
            return { ...lane, streaming: '' };
        case 'llm_thought':
        case 'llm_reasoning': {
            // What it is thinking, in its own words, as the line says while it works; a tool call's JSON is
            // not a thought.
            const said = firstLine(String(inner.thought ?? inner.reasoning ?? ''));
            return said ? { ...lane, activity: said } : lane;
        }
        default:
            return lane;
    }
}

/** A thought's first line, short enough for one line; nothing for a tool call's JSON. */
function firstLine(text: string): string {
    const line = text.trim().split('\n').find((l) => l.trim())?.trim() ?? '';
    if (!line || line.startsWith('{') || line.startsWith('[')) return '';
    return line.length > 120 ? `${line.slice(0, 117)}…` : line;
}

/** "1 min 40 s", "12 s". */
export function helperDuration(seconds?: number): string {
    if (seconds === undefined || seconds < 0) return '';
    const whole = Math.round(seconds);
    if (whole < 60) return `${whole} s`;
    const minutes = Math.floor(whole / 60);
    const rest = whole % 60;
    return rest ? `${minutes} min ${rest} s` : `${minutes} min`;
}

/** The lane's one status line. */
export function helperStatusLine(lane: HelperLaneData): string {
    const calls = lane.calls.length;
    const did = calls ? `${calls} call${calls === 1 ? '' : 's'}` : '';
    const took = helperDuration(lane.seconds);
    const facts = [did, took].filter(Boolean).join(' · ');
    switch (lane.status) {
        case 'running':
            return lane.mode === 'background' ? 'Working in the background' : 'Working';
        case 'completed':
            return facts ? `Finished · ${facts}` : 'Finished';
        case 'cancelled':
            return facts ? `Stopped · ${facts}` : 'Stopped';
        case 'timed_out':
            return facts ? `Ran out of time · ${facts}` : 'Ran out of time';
        default:
            return lane.error ? `Failed: ${lane.error}` : 'Failed';
    }
}

/** What "Save as agent" opens the agent editor with: its brief as instructions, its tools and its model (S9). */
export function agentFromHelper(lane: HelperLaneData): { name: string; system_prompt: string; tools: string[]; model: string | null } {
    return { name: lane.name, system_prompt: lane.task, tools: [...lane.tools], model: lane.model || null };
}

/** The tool that sends a helper. Its lane says what the call did, so the tools-used row leaves it out (F572). */
export const HELPER_TOOL = 'spawn_subtask';

/**
 * The calls a tools-used row lists beside a turn's helper lanes (F572): the same calls, less one
 * `spawn_subtask` for each lane drawn. A refused send stays — it has no lane, and "not run" is
 * news (F570) — and with no lanes nothing is left out. Each product's call names its tool as
 * `tool` (self-hosted's saved call) or `name` (cloud's); the saved calls themselves keep every
 * send, for the run log and History.
 */
export function toolsUsedBesideLanes<T extends { tool?: string; name?: string; refused?: boolean }>(
    calls: T[],
    lanes: number,
): T[] {
    let left = lanes;
    return calls.filter((call) => {
        if (left > 0 && (call.tool ?? call.name) === HELPER_TOOL && !call.refused) {
            left -= 1;
            return false;
        }
        return true;
    });
}
