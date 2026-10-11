/*
 * The Phone tab's shapes and words (VOX1c): a number, its calls, a call's log.
 *
 * Pure: no fetch, no store, no React. Each product's host (`host.tsx`) reads
 * its own API and hands the screens these shapes; cloud's Vitest reads the
 * helpers (`web/src/lib/phone.test.ts`). Both APIs return the self-hosted
 * engine's records (VOX1a's `voice_calls` record, a `messaging_channels`
 * document), so one mapper serves both.
 */

export const PLATFORM = 'twilio_voice';

/** What a person sets on a number's `voice` block. The APIs replace the block whole, so all of them are sent. */
export type VoiceSettings = {
    notice: string;
    filler: string;
    filler_late: string;
    transfer_number: string;
    recording: boolean;
    language: string;
    voice: string;
    tts_provider: string;
    max_minutes: number | null;
    /** VOX1d's: sent only where the options say the API takes them (`speech_models`). */
    transcription_provider?: string;
    speech_model?: string;
};

/** A number's `voice` block as read: the settings, plus what connecting wrote. */
export type VoiceBlock = Partial<VoiceSettings> & {
    number?: string;
    number_sid?: string;
    /** The SharedKey's ids when webhooks are signed (a Standard key). */
    signing?: Record<string, string> | null;
    /** Why they are not (a Restricted key): calls still work. */
    signing_error?: string;
    /** V7: the key could not point the number, so a person pastes this into Twilio. */
    manual_url?: string;
    point_error?: string;
    [key: string]: unknown;
};

export type PhoneNumber = {
    id: string;
    name: string;
    enabled: boolean;
    /** `running`, `stopped`, `error`. */
    status: string | null;
    lastError: string | null;
    agentIds: string[];
    visitorTools: string[];
    voice: VoiceBlock;
};

export type CallRecord = {
    /** The CallSid. */
    id: string;
    channelId: string;
    agentId: string;
    sessionId: string;
    caller: string;
    called: string;
    startedAt: string | null;
    endedAt: string | null;
    seconds: number;
    outcome: string | null;
    transfer: { reason?: string; status?: string } | null;
    voicemail: { recording_sid?: string; url?: string; seconds?: number } | null;
    recordingSid: string | null;
    approvals: string[];
    error: string | null;
    firstWordsMs: number[];
    /** Cloud: the call's one charge, in millicredits; null until settled. */
    chargedMc: number | null;
    settledAt: string | null;
};

/** A tool the agent called in a turn. */
export type ToolLine = {
    name: string;
    args: Record<string, unknown> | null;
    result: string | null;
    refused: boolean;
    reason: string | null;
};

/** One exchange: what the caller said and what the agent answered. */
export type CallTurn = {
    said: string;
    answer: string;
    /** When the answer was saved. */
    at: string | null;
    tools: ToolLine[];
    /** What the call said while a tool ran (V13). */
    filler: string[];
    /** The caller spoke over the answer: it is what they heard (V14). */
    interrupted: boolean;
};

/** The question a call parked in the bell and nobody has answered yet (V16). */
export type PendingAsk = { tool: string; args: Record<string, unknown> };

export type Transcript = { turns: CallTurn[]; pending: PendingAsk | null };

export type PhoneAgent = {
    id: string;
    name: string;
    /** The model it answers with: its own, else the workspace's. */
    model: string;
    /** Its reasoning effort, its own or the workspace's: `off`, `low`, `medium`, `high`. */
    thinking: string;
    planning: boolean;
    /** It runs a workflow (an orchestrator agent). */
    workflow: boolean;
};

/** A language a number may speak (VOX1d's `languages.public()`). */
export type PhoneLanguage = {
    code: string;
    /** "English (India)", "Hindi". */
    name: string;
    /** The text-to-speech providers that speak it. */
    tts: string[];
    /** The transcription providers that hear it, each with its models. */
    stt: Record<string, string[]>;
    /** What hears it when the number chooses nothing. */
    default_stt: { provider: string; model: string } | null;
};

export type PhoneOptions = {
    /** Whether Twilio can reach this server at all; `reason` says what to do when not. */
    available: boolean;
    reason: string;
    languages: PhoneLanguage[];
    /** What an unset language means where the number's country says nothing (VOX1d: by its country code). */
    default_language: string;
    tts_providers: string[];
    /** VOX1d's transcription choice; both absent where the API does not take it yet. */
    transcription_providers?: string[];
    speech_models?: Record<string, string[]>;
    /** Model-id prefixes fast enough for a call (V15). */
    fast_for_voice: string[];
};

export const EMPTY_OPTIONS: PhoneOptions = {
    available: true, reason: '', languages: [], default_language: 'en-US', tts_providers: [], fast_for_voice: [],
};

/** A number on the Twilio account, at step two. */
export type TwilioNumber = { sid: string; number: string; voice_url: string; voice_application_sid: string };

export type TwilioKey = { accountSid: string; keySid: string; keySecret: string };

// ── Reading the APIs ─────────────────────────────────────────────────────────

const str = (v: unknown): string => (typeof v === 'string' ? v : '');
const strOrNull = (v: unknown): string | null => (typeof v === 'string' && v ? v : null);
const list = (v: unknown): unknown[] => (Array.isArray(v) ? v : []);
const obj = (v: unknown): Record<string, unknown> | null =>
    v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : null;

/** A channel document (either product's view of it) as a number. */
export function numberFromChannel(raw: unknown): PhoneNumber {
    const ch = obj(raw) ?? {};
    return {
        id: str(ch.id),
        name: str(ch.name),
        enabled: ch.enabled !== false,
        status: strOrNull(ch.status),
        lastError: strOrNull(ch.last_error),
        agentIds: list(ch.agent_ids).map(String),
        visitorTools: list(ch.visitor_tools).map(String),
        voice: (obj(ch.voice) ?? {}) as VoiceBlock,
    };
}

/** A call's record (VOX1a's shape; cloud adds `charged_mc` and `settled_at`). */
export function callFromRecord(raw: unknown): CallRecord {
    const r = obj(raw) ?? {};
    const segments = list(r.segments).map(s => Number(obj(s)?.seconds) || 0);
    const summed = segments.reduce((a, b) => a + b, 0);
    return {
        id: str(r.id),
        channelId: str(r.channel_id),
        agentId: str(r.agent_id),
        sessionId: str(r.session_id),
        caller: str(r.caller),
        called: str(r.called),
        startedAt: strOrNull(r.started_at),
        endedAt: strOrNull(r.ended_at),
        seconds: Number(r.seconds) || summed || Number(r.call_seconds) || 0,
        outcome: strOrNull(r.outcome),
        transfer: obj(r.transfer) as CallRecord['transfer'],
        voicemail: obj(r.voicemail) as CallRecord['voicemail'],
        recordingSid: strOrNull(r.recording_sid),
        approvals: list(r.approvals).map(String),
        error: strOrNull(r.error),
        firstWordsMs: list(r.first_words_ms).map(Number).filter(n => Number.isFinite(n)),
        chargedMc: typeof r.charged_mc === 'number' ? r.charged_mc : null,
        settledAt: strOrNull(r.settled_at),
    };
}

function toolLines(calls: unknown, names: unknown): ToolLine[] {
    const detailed = list(calls).map(c => {
        const call = obj(c) ?? {};
        return {
            name: str(call.tool) || str(call.name),
            args: obj(call.args),
            result: call.result == null ? null : typeof call.result === 'string' ? call.result : JSON.stringify(call.result),
            refused: call.refused === true,
            reason: strOrNull(call.reason),
        };
    }).filter(t => t.name);
    if (detailed.length) return detailed;
    return list(names).map(String).filter(Boolean).map(name => ({ name, args: null, result: null, refused: false, reason: null }));
}

function turnFrom(said: unknown, answer: Record<string, unknown>, at: unknown): CallTurn {
    return {
        said: str(said),
        answer: str(answer.content ?? answer.assistant),
        at: strOrNull(at),
        tools: toolLines(answer.calls, answer.tools),
        filler: list(answer.filler).map(String),
        interrupted: answer.interrupted === true,
    };
}

/** Self-hosted: `GET /api/sessions/{id}/history`'s turns. */
export function turnsFromHistory(turns: unknown): CallTurn[] {
    return list(turns).map(t => {
        const turn = obj(t) ?? {};
        return turnFrom(turn.user, turn, turn.timestamp);
    });
}

/** Cloud: `GET /chat/sessions/{id}`'s messages, a question then its answer. */
export function turnsFromMessages(messages: unknown): CallTurn[] {
    const out: CallTurn[] = [];
    const all = list(messages).map(m => obj(m) ?? {});
    for (let i = 0; i + 1 < all.length; i++) {
        if (all[i].role === 'user' && all[i + 1].role === 'assistant') {
            out.push(turnFrom(all[i].content, all[i + 1], all[i + 1].timestamp));
            i++;
        }
    }
    return out;
}

/** The parked question either API returns beside the turns, or null. */
export function pendingFrom(raw: unknown): PendingAsk | null {
    const p = obj(raw);
    if (!p || !str(p.tool_name)) return null;
    return { tool: str(p.tool_name), args: obj(p.tool_args) ?? {} };
}

// ── Words ────────────────────────────────────────────────────────────────────

export type Tone = 'machine' | 'person' | 'success' | 'danger' | 'neutral';

const OUTCOMES: Record<string, { word: string; tone: Tone }> = {
    caller_hung_up: { word: 'Ended by the caller', tone: 'success' },
    agent_ended: { word: 'Ended by the agent', tone: 'success' },
    transferred: { word: 'Transferred to a person', tone: 'neutral' },
    voicemail: { word: 'Left a voicemail', tone: 'person' },
    limit: { word: 'Reached the longest call', tone: 'neutral' },
    silence: { word: 'Ended after silence', tone: 'neutral' },
    refused: { word: "Couldn't be answered", tone: 'danger' },
    failed: { word: 'Failed', tone: 'danger' },
};

/** Whether the call is still on the line (a moved call is picked up on another server). */
export function isLive(call: CallRecord): boolean {
    return !call.endedAt && (call.outcome === null || call.outcome === 'moved');
}

/**
 * How a call ended, in a few words, and its colour: a person is needed (an
 * approval, a voicemail) is amber; a failure red; a call that went well quiet.
 */
export function outcomeOf(call: CallRecord): { word: string; tone: Tone } {
    if (isLive(call)) return { word: 'On the line now', tone: 'machine' };
    if (call.voicemail || call.outcome === 'voicemail') return OUTCOMES.voicemail;
    if (call.approvals.length) return { word: 'Approval asked', tone: 'person' };
    return (call.outcome && OUTCOMES[call.outcome]) || { word: 'Ended', tone: 'success' };
}

/** The outcome as a sentence, for the call's own head: "Approval asked, then ended by the caller". */
export function outcomeSentence(call: CallRecord): string {
    const base = (call.outcome && OUTCOMES[call.outcome]?.word) || (isLive(call) ? 'On the line now' : 'Ended');
    if (call.approvals.length && !isLive(call)) return `Approval asked, then ${base.charAt(0).toLowerCase()}${base.slice(1)}`;
    return base;
}

/** A browser call (Twilio's Voice SDK) has a client identity, not a number. */
export function callerLabel(caller: string): string {
    if (!caller) return 'Unknown caller';
    if (caller.startsWith('client:')) return `Browser call (${caller.slice(7) || 'unnamed'})`;
    return caller;
}

/** A call's length so far, in seconds: its record's, or the time since it started while it is on the line. */
export function callSeconds(call: CallRecord, now: number): number {
    if (!isLive(call) || !call.startedAt) return call.seconds;
    const started = Date.parse(call.startedAt);
    return Number.isNaN(started) ? call.seconds : Math.max(call.seconds, Math.round((now - started) / 1000));
}

/** "3:12", "1:02:05". */
export function clockLength(seconds: number): string {
    const s = Math.max(0, Math.round(seconds));
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const pad = (n: number) => String(n).padStart(2, '0');
    return h ? `${h}:${pad(m)}:${pad(s % 60)}` : `${m}:${pad(s % 60)}`;
}

/** Started minutes: how a call is charged, ours and Twilio's (V21). */
export function startedMinutes(seconds: number): number {
    return seconds > 0 ? Math.ceil(seconds / 60) : 0;
}

/** What a call cost us in credits (cloud), or null until it is settled. */
export function credits(call: CallRecord): number | null {
    return call.chargedMc === null ? null : call.chargedMc / 1000;
}

/** Twilio's US prices per minute, read 2026-10-10 (19 → V21; `drafts/vox0/sources.md`). An estimate, said so. */
export const TWILIO_US = { relay: 0.07, inboundLocal: 0.0085, recording: 0.0025 } as const;

/**
 * Twilio's charge for a call at US local rates: ConversationRelay and the
 * inbound minute for each started minute, plus the recording when there was
 * one. A browser call has no phone leg. It is on the customer's Twilio bill.
 */
export function twilioEstimate(call: CallRecord): number {
    const minutes = startedMinutes(call.seconds);
    const inbound = call.caller.startsWith('client:') ? 0 : TWILIO_US.inboundLocal;
    const recording = call.recordingSid ? TWILIO_US.recording : 0;
    return Math.round(minutes * (TWILIO_US.relay + inbound + recording) * 10000) / 10000;
}

/** The middle of a call's first-words times, in seconds (V12: the caller's last word in, our first out). */
export function firstWords(call: CallRecord): number | null {
    const ms = [...call.firstWordsMs].sort((a, b) => a - b);
    if (!ms.length) return null;
    const mid = Math.floor(ms.length / 2);
    const median = ms.length % 2 ? ms[mid] : (ms[mid - 1] + ms[mid]) / 2;
    return Math.round(median / 100) / 10;
}

/** Whether `model` is fast enough for a call (V15): a prefix of the list the API serves. */
export function fastForVoice(model: string, prefixes: readonly string[]): boolean {
    const name = model.trim().toLowerCase();
    return !!name && prefixes.some(p => name.startsWith(p.toLowerCase()));
}

/**
 * The quiet notes beside the answering agent (V15, D248, D251): never a
 * warning, never a block. A slow model, thinking or planning on, and a
 * workflow each make a caller wait.
 */
export function agentNotes(agent: PhoneAgent | undefined, fast: readonly string[]): string[] {
    if (!agent) return [];
    const notes: string[] = [];
    if (agent.workflow) {
        notes.push(`${agent.name} runs a workflow. On the phone it can be slow unless it replies as it goes.`);
    }
    if (agent.model && fast.length && !fastForVoice(agent.model, fast)) {
        notes.push(`${agent.name} runs on ${agent.model}. On a call a faster model answers sooner, such as one on Groq or Cerebras, or Nova Micro or Lite.`);
    }
    const thinking = agent.thinking && agent.thinking !== 'off';
    if (thinking || agent.planning) {
        const on = thinking && agent.planning ? `Thinking (${agent.thinking}) and planning are` : thinking ? `Thinking (${agent.thinking}) is` : 'Planning is';
        notes.push(`${on} on for ${agent.name}. For a phone agent turn ${thinking && agent.planning ? 'both' : 'it'} off on the Agents page: it answers sooner, and a weak model doesn't loop on its plan.`);
    }
    return notes;
}

/** E.164, as Twilio dials a transfer (cloud's API holds the same rule). */
export const E164 = /^\+[1-9]\d{6,14}$/;

/** The settings in a number's block, with the defaults the call uses for anything unset. */
export function settingsOf(voice: VoiceBlock): VoiceSettings {
    return {
        notice: str(voice.notice),
        filler: str(voice.filler),
        filler_late: str(voice.filler_late),
        transfer_number: str(voice.transfer_number),
        recording: voice.recording === true,
        language: str(voice.language),
        voice: str(voice.voice),
        tts_provider: str(voice.tts_provider),
        max_minutes: typeof voice.max_minutes === 'number' ? voice.max_minutes : null,
        transcription_provider: str(voice.transcription_provider),
        speech_model: str(voice.speech_model),
    };
}

/** What a save sends: every setting, VOX1d's two only where the API takes them. */
export function settingsToSend(s: VoiceSettings, options: Pick<PhoneOptions, 'speech_models'>): VoiceSettings {
    const { transcription_provider, speech_model, ...rest } = s;
    return options.speech_models
        ? { ...rest, transcription_provider: transcription_provider ?? '', speech_model: speech_model ?? '' }
        : rest;
}

/** The default notice's words (OSS `service.DEFAULT_NOTICE`): `{agent}` and `{org}` are filled in on the call. */
export const DEFAULT_NOTICE = "Hi, you're speaking with {agent}, an AI assistant for {org}.";
export const DEFAULT_FILLER = 'One moment while I check that.';
export const DEFAULT_FILLER_LATE = 'Still working on it.';

/** The six permissions a Restricted key needs (19 → Probes, P2); the recordings one only with recording on. */
export const RESTRICTED_PERMISSIONS = [
    '/twilio/voice/calls/read',
    '/twilio/voice/calls/update',
    '/twilio/voice/calls.recordings/create',
    'twilio/phone-numbers/active-numbers/list',
    'twilio/phone-numbers/active-numbers/read',
    'twilio/phone-numbers/active-numbers/update',
] as const;

export const TWILIO_KEYS_URL = 'https://console.twilio.com/us1/account/keys-credentials/api-keys';

/** The channel and CallSid a voice session names (`voice_{channel}_{CallSid}`; channel ids hold underscores). */
export function parseVoiceSession(sessionId: string): { channelId: string; callSid: string } | null {
    if (!sessionId.startsWith('voice_')) return null;
    const rest = sessionId.slice(6);
    const at = rest.lastIndexOf('_');
    if (at <= 0) return null;
    return { channelId: rest.slice(0, at), callSid: rest.slice(at + 1) };
}

/** Where a bell item about a call opens: the number with that call's log over it. */
export function callHref(channelId: string, callSid?: string): string {
    const q = new URLSearchParams({ number: channelId });
    if (callSid) q.set('call', callSid);
    return `/channels/phone?${q.toString()}`;
}

/**
 * Where a bell item about a call opens (VOX1c): voicemail and a transfer carry the call; a caller's parked
 * approval names its session; a refused call names only its number. Null for anything that is not a call's.
 */
export function voiceItemHref(data: Record<string, unknown> | null | undefined): string | null {
    if (!data || data.door !== 'voice') return null;
    const parsed = typeof data.session_id === 'string' ? parseVoiceSession(data.session_id) : null;
    const channel = (typeof data.channel_id === 'string' && data.channel_id) || parsed?.channelId;
    if (!channel) return null;
    const callSid = (typeof data.call_sid === 'string' && data.call_sid) || parsed?.callSid;
    return callHref(channel, callSid || undefined);
}

/** A select's "nothing chosen" (the language's default, Twilio's voice): the kit's Select can't hold an empty value. */
export const UNSET = '__unset__';
export const toChoice = (value: string | undefined | null): string => value || UNSET;
export const fromChoice = (choice: string): string => (choice === UNSET ? '' : choice);

/** An error as a person reads it: a save's 422 carries pydantic's "Value error, " before the sentence (VOX1d's check). */
export function errorWords(e: unknown, fallback: string): string {
    const message = e instanceof Error ? e.message : '';
    return message.replace(/(^|\. )Value error, /g, '$1').trim() || fallback;
}
