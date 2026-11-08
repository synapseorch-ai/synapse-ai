import type { BrowserAction } from '@/components/browser/model';
import type { HelperLaneData } from '@/components/assistant/helpers';
import type { TeamState } from '@/components/team/team';

/* eslint-disable @typescript-eslint/no-explicit-any */
import type { KnowledgeCitation } from '@/lib/knowledge';

// Defined beside the Knowledge client so `lib/knowledge.ts` imports nothing from here and syncs to the
// cloud unchanged (R1c, D173); re-exported so `@/types` stays the one place app code reads it from.
export type { KnowledgeCitation };

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
    // The tools the answer's turn called, gathered from its live events, so a
    // turn shows its tool chip before a reload as well as after (M2c). A
    // reopened turn has the same list, saved, in `data.calls`.
    // A browser tool's call carries what it did in the page, with its screenshot (BRW2, F1074).
    toolCalls?: { tool: string; args: Record<string, unknown> | null; result: string; refused?: boolean; reason?: string;
                  browser?: BrowserAction }[];
    // The helpers its turn sent (SUB1a): each one's lane, live or as saved.
    helpers?: HelperLaneData[];
    // A Team agent's turn (TEAM1b): its task list, notes and members' lanes, live or as saved.
    team?: TeamState;
    // What the turn warned of beside its answer (F584): a board card it left claimed (F582), live or as saved.
    notes?: string[];
}

export interface SystemStatus {
    agents: Record<string, {
        status: string;
        name: string;
        can_see_images?: boolean;
        /** Why Attach is off for a workflow agent: no step takes attachments, or one can't read them (F313). */
        attach_problem?: string | null;
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
