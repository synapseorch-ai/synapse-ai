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
    | 'human_input_required';

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
}

export interface SystemStatus {
    agents: Record<string, { status: string; name: string }>;
    overall: 'operational' | 'degraded' | 'down';
    model: string;
    mode: 'local' | 'cloud' | 'bedrock';
    active_agent_id?: string;
    provider?: string;
}
