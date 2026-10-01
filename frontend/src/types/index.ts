/* eslint-disable @typescript-eslint/no-explicit-any */
export interface Email {
    id: string;
    sender: string;
    subject: string;
    snippet: string;
}

export interface FullEmail {
    id: string;
    sender: string;
    subject: string;
    date: string;
    body: string;
    html_body?: string;
}

export interface DriveFile {
    id: string;
    name: string;
    webViewLink: string;
    iconLink?: string;
}

export interface CalendarEvent {
    id: string;
    summary: string;
    start: string;
    htmlLink: string;
}

export interface LocalFile {
    name: string;
    path: string;
    size: number;
}

export type OrchMsgType =
    | 'regular'
    | 'orchestration_start'
    | 'step_start'
    | 'agent_step_result'
    | 'orchestration_complete'
    | 'human_input_required'
    /** A quiet line in the transcript: something the loop did to keep going.
     *  Not the agent speaking, so it is not a bubble and carries no avatar. */
    | 'note';

export interface Message {
    role: 'user' | 'assistant';
    content: string;
    intent?: 'chat' | 'list_emails' | 'read_email' | 'list_files' | 'list_events' | 'request_auth' | 'list_local_files' | 'render_local_file' | 'draft_email' | 'send_email' | 'custom_tool' | 'collect_data' | 'orchestration';
    data?: any;
    tool?: string;
    // Orchestration fields
    msgType?: OrchMsgType;
    stepName?: string;
    stepType?: string;
    orchStepId?: string;
    thoughts?: string[];
    // Per-turn private reasoning extracted from [REASONING] blocks. One entry
    // per turn the model produced a [REASONING] block, in chronological order.
    reasoning?: string[];
    // Attached images (base64 data URIs) — user messages only
    images?: string[];
    // Calls this answer's turn did not run, and why (U68) — the answer is the
    // model's words and can still say "done".
    refused?: { tool: string; reason: string }[];
    // The answer's Sources: its [K#] labels resolved by the server (Knowledge,
    // B1b), and the labels it used that no search in its turn produced.
    citations?: KnowledgeCitation[];
    unresolvedCitations?: string[];
}

/** A `[K#]` label resolved to where the words are (`core.knowledge.api_models.Citation`). */
export interface KnowledgeCitation {
    label: string;
    kb_id: string;
    doc_id: string;
    chunk_id: string;
    path: string;
    source: string;
    source_url?: string | null;
    title?: string | null;
    page_start?: number | null;
    page_end?: number | null;
    char_start: number;
    char_end: number;
    heading_path: string[];
    snippet: string;
    indexed_at?: string | null;
    snapshot_id: string;
    /** Whether the words the answer quoted for this label are in its passage. */
    quote_check?: { quote: string; matched: boolean; method: 'exact' | 'normalized' } | null;
}

export interface SystemStatus {
    agents: Record<string, {
        status: string;
        name: string;
        can_see_images?: boolean;
        /** `orchestrator` for a workflow agent, whose steps each have their own model (U64). */
        type?: string;
        orchestration_id?: string | null;
    }>;
    overall: 'operational' | 'degraded' | 'down';
    model: string;
    mode: 'local' | 'cloud' | 'bedrock';
    active_agent_id?: string;
    provider?: string;
    /** Whether the model the page will use can answer at all (F182). Nothing paid is tried. */
    model_ready?: boolean;
    /** Why it cannot, in a sentence; null when nothing is known to stop it. */
    model_problem?: string | null;
}
